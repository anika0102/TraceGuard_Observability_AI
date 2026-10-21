"""TraceGuard API: simulated recommendation workflow with tracing, metrics and outcome validation."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import domain, metrics, telemetry
from .config import Settings
from .faults import FaultState
from .incident_detection import INCIDENT_TYPES, IncidentDetector, Sample
from .schemas import (Correctness, FaultRequest, ProductOut, RecommendRequest, RecommendResponse, Scenario)

log = logging.getLogger("traceguard.app")


class InvalidInput(Exception):
    pass


class UpstreamFailure(Exception):
    """Injected failure in the recommendation step."""


def _product_out(p: Optional[domain.Product]) -> Optional[ProductOut]:
    return ProductOut(sku=p.sku, name=p.name, category=p.category, price=p.price, score=p.score) if p else None


def _hash_user(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]  # pseudonymised; never a metric label


def _hard_exit() -> None:  # pragma: no cover - replaced in tests
    os._exit(1)


def create_app(settings: Optional[Settings] = None, extra_span_exporters=()) -> FastAPI:
    settings = settings or Settings.from_env()
    telemetry.configure_logging()
    provider = telemetry.init_tracing(settings, extra_span_exporters)
    tracer = provider.get_tracer("traceguard.workflow")

    app = FastAPI(title="TraceGuard", version=settings.service_version)
    app.state.settings = settings
    app.state.faults = FaultState()
    app.state.detector = IncidentDetector(settings)
    app.state.crash_fn: Callable[[], None] = _hard_exit
    app.state.started = True
    faults: FaultState = app.state.faults
    detector: IncidentDetector = app.state.detector

    if settings.enable_fault_injection and not settings.fault_injection_allowed:
        log.warning("fault injection flag set but environment=%s; fault injection stays DISABLED",
                    settings.environment)

    def is_ready() -> bool:
        return app.state.started and faults.scenario is not Scenario.not_ready

    def run_detection() -> None:
        for inc in detector.evaluate(ready=is_ready()):
            metrics.INCIDENTS.labels(type=inc.type, severity=inc.severity).inc()
            log.warning("incident opened", extra={"fields": {
                "incident_type": inc.type, "severity": inc.severity, "incident_trace_id": inc.trace_id}})
        active_types = {i.type for i in detector.active()}
        for t in INCIDENT_TYPES:
            metrics.ACTIVE_INCIDENTS.labels(type=t).set(1 if t in active_types else 0)

    @contextmanager
    def operation(name: str):
        """Child span with operation name, duration and status attributes."""
        start = time.perf_counter()
        with tracer.start_as_current_span(f"recommend.{name}") as span:
            span.set_attribute("traceguard.operation", name)
            span.set_attribute("traceguard.fault_scenario", faults.scenario.value)
            try:
                yield span
                span.set_attribute("traceguard.status", "ok")
            except Exception as exc:
                span.set_attribute("traceguard.status", "error")
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                span.set_attribute("traceguard.duration_ms", round((time.perf_counter() - start) * 1000, 2))

    # -- generic HTTP metrics ------------------------------------------------------------
    @app.middleware("http")
    async def http_metrics(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", "unmatched")  # template, never the raw path
            metrics.HTTP_REQUESTS.labels(template, request.method, str(status)).inc()
            metrics.HTTP_DURATION.labels(template, request.method).observe(time.perf_counter() - start)

    # -- probes --------------------------------------------------------------------------
    @app.get("/health")
    def health():
        if faults.scenario is Scenario.unhealthy:
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        ok = is_ready()
        metrics.READY.set(1 if ok else 0)
        run_detection()  # lets a readiness incident open/close even with no traffic
        return JSONResponse({"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503)

    @app.get("/metrics")
    def prom_metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # -- business endpoint ---------------------------------------------------------------
    @app.post("/recommend", response_model=RecommendResponse)
    async def recommend(req: RecommendRequest):
        start = time.perf_counter()
        scenario = faults.scenario
        server_span = trace.get_current_span()
        server_span.set_attribute("traceguard.fault_scenario", scenario.value)
        trace_id = telemetry.current_trace_id()
        status_code, correct, body = 200, None, None

        try:
            with operation("validate_input") as sp:
                sp.set_attribute("traceguard.user_hash", _hash_user(req.user_id))
                sp.set_attribute("traceguard.category", req.category)
                if req.category not in domain.CATEGORIES:
                    raise InvalidInput(f"unknown category '{req.category}'")

            with operation("lookup_catalog") as sp:  # simulated database / tool lookup
                if scenario is Scenario.slow:
                    delay = faults.delay_ms or settings.slow_fault_delay_ms
                    sp.set_attribute("traceguard.injected_delay_ms", delay)
                    await asyncio.sleep(delay / 1000)
                found = domain.candidates(req.category, req.budget)
                sp.set_attribute("traceguard.candidate_count", len(found))

            with operation("compute_recommendation") as sp:
                if faults.should_error():
                    raise UpstreamFailure("injected failure in recommendation model")
                ranked = domain.rank(found)
                # wrong_result: a stale ranking serves the runner-up while everything looks healthy.
                pick = ranked[1] if scenario is Scenario.wrong_result and len(ranked) > 1 else (
                    ranked[0] if ranked else None)
                sp.set_attribute("traceguard.actual_sku", pick.sku if pick else "none")

            with operation("validate_business_rules") as sp:
                expected = domain.expected_recommendation(req.category, req.budget)
                exp_sku, act_sku = (expected.sku if expected else None), (pick.sku if pick else None)
                # Invariants (budget, stock) plus comparison with the golden reference.
                invariants_ok = pick is None or (pick.price <= req.budget and pick.in_stock)
                correct = invariants_ok and exp_sku == act_sku
                sp.set_attribute("traceguard.expected_sku", exp_sku or "none")
                sp.set_attribute("traceguard.actual_sku", act_sku or "none")
                sp.set_attribute("traceguard.outcome_correct", correct)
                if not correct:
                    sp.add_event("business_outcome_mismatch",
                                 {"expected_sku": exp_sku or "none", "actual_sku": act_sku or "none"})
                metrics.CORRECTNESS.labels("correct" if correct else "incorrect").inc()
                server_span.set_attribute("traceguard.outcome_correct", correct)

            with operation("build_response"):
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                body = RecommendResponse(
                    trace_id=trace_id,
                    recommendation=_product_out(pick),
                    correctness=Correctness(
                        checked=True, correct=correct, expected_sku=exp_sku, actual_sku=act_sku,
                        detail="matches expected business result" if correct
                        else "HTTP 200 but result differs from expected business result"),
                    fault_scenario=scenario.value,
                    latency_ms=latency_ms,
                )
        except InvalidInput as exc:
            status_code = 400
            server_span.set_status(Status(StatusCode.ERROR, "invalid input"))
            result = JSONResponse({"error": "invalid_input", "detail": str(exc), "trace_id": trace_id}, 400)
        except UpstreamFailure as exc:
            status_code = 500
            server_span.set_status(Status(StatusCode.ERROR, str(exc)))
            log.error("recommendation failed", extra={"fields": {"fault_scenario": scenario.value}})
            result = JSONResponse({"error": "upstream_failure", "detail": str(exc), "trace_id": trace_id}, 500)
        else:
            result = JSONResponse(body.model_dump(), 200)
            log.info("recommendation served", extra={"fields": {
                "fault_scenario": scenario.value, "outcome_correct": correct}})

        duration_ms = (time.perf_counter() - start) * 1000
        server_span.set_attribute("traceguard.duration_ms", round(duration_ms, 2))
        metrics.RECOMMEND_REQUESTS.labels("ok" if status_code < 400 else ("client_error" if status_code < 500
                                                                           else "error"), scenario.value).inc()
        detector.record(Sample(ts=time.time(), duration_ms=duration_ms, status_code=status_code,
                               correct=correct, trace_id=trace_id, scenario=scenario.value))
        run_detection()
        if trace_id:
            result.headers["X-Trace-Id"] = trace_id
        return result

    # -- fault injection (local development only) -----------------------------------------
    @app.get("/simulate-failure")
    def fault_status():
        return {"enabled": settings.fault_injection_allowed, "environment": settings.environment,
                **faults.as_dict()}

    @app.post("/simulate-failure")
    def simulate_failure(req: FaultRequest):
        if not settings.fault_injection_allowed:
            raise HTTPException(403, "fault injection is disabled (requires TRACEGUARD_ENV=local and "
                                     "TRACEGUARD_ENABLE_FAULT_INJECTION=true)")
        if req.clear_incident_window:
            detector.clear_window()
        if req.scenario is Scenario.crash:
            log.warning("crash scenario: process exits in 1s")
            threading.Timer(1.0, lambda: app.state.crash_fn()).start()
            return {"scenario": "crash", "detail": "process will exit in ~1s; Kubernetes restarts the container"}
        faults.set(req.scenario, delay_ms=req.delay_ms or 0, error_every=req.error_every)
        for sc in Scenario:
            metrics.FAULT_ACTIVE.labels(sc.value).set(1 if sc is req.scenario else 0)
        run_detection()
        log.warning("fault scenario set", extra={"fields": {"fault_scenario": req.scenario.value}})
        return faults.as_dict()

    # -- incidents -----------------------------------------------------------------------
    @app.get("/incidents")
    def incidents():
        run_detection()
        return {"window": detector.stats(),
                "thresholds": {"latency_p95_ms": settings.latency_p95_threshold_ms,
                               "error_rate": settings.error_rate_threshold,
                               "incorrect_rate": settings.correctness_failure_threshold},
                "active": [i.as_dict() for i in detector.active()],
                "history": [i.as_dict() for i in detector.history()],
                "jaeger_ui_url": settings.jaeger_ui_url}

    @app.get("/console", response_class=HTMLResponse)
    def console():
        return CONSOLE_HTML

    for sc in Scenario:
        metrics.FAULT_ACTIVE.labels(sc.value).set(1 if sc is Scenario.none else 0)
    for t in INCIDENT_TYPES:
        metrics.ACTIVE_INCIDENTS.labels(t).set(0)
    metrics.READY.set(1)

    telemetry.instrument_app(app, provider)
    return app


CONSOLE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>TraceGuard incidents</title>
<style>body{font:14px system-ui;margin:2rem;max-width:1000px}table{border-collapse:collapse;width:100%}
td,th{border:1px solid #ccc;padding:6px;text-align:left;vertical-align:top}.critical{background:#fde2e2}
.warning{background:#fff3cd}.resolved{opacity:.55}code{font-size:12px}</style></head><body>
<h2>TraceGuard incident console</h2><p id="win">loading...</p>
<table><thead><tr><th>Detected</th><th>Type</th><th>Severity</th><th>Status</th><th>Summary / evidence</th>
<th>Trace</th></tr></thead><tbody id="rows"></tbody></table>
<p><small>Live data from <code>/incidents</code> (refreshes every 3 s). Incidents are detected symptoms; a trace
is an example affected request, not a confirmed root cause.</small></p>
<script>
async function load(){const r=await fetch('/incidents');const d=await r.json();const w=d.window;
document.getElementById('win').textContent=`window: ${w.samples} requests | error rate ${(w.error_rate*100).toFixed(0)}% | `+
`p50 ${w.p50_ms} ms | p95 ${w.p95_ms} ms | incorrect ${(w.incorrect_rate*100).toFixed(0)}% of ${w.checked} checked`;
const rows=[...d.history].reverse().map(i=>{const t=i.trace_id?`<a target=_blank href="${d.jaeger_ui_url}/trace/${i.trace_id}">${i.trace_id}</a>`:'n/a';
return `<tr class="${i.severity} ${i.status}"><td>${i.detected_at_iso}</td><td>${i.type}</td><td>${i.severity}</td><td>${i.status}</td>`+
`<td>${i.summary}<br><code>${JSON.stringify(i.evidence)}</code></td><td>${t}</td></tr>`}).join('');
document.getElementById('rows').innerHTML=rows||'<tr><td colspan=6>no incidents</td></tr>'}
load();setInterval(load,3000);</script></body></html>"""

app = create_app()
