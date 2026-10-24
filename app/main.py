"""TraceGuard API: simulated recommendation workflow with tracing, metrics and outcome validation."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import metrics, telemetry, ui_api
from .config import Settings
from .faults import FaultState
from .incident_detection import INCIDENT_TYPES, IncidentDetector, Sample
from .metrics_history import MetricsHistory
from .pipeline import RecommendPipeline, record_request_metric
from .ratelimit import RateLimiter
from .sandbox import SandboxRunner
from .schemas import FaultRequest, RecommendRequest, RecommendResponse, Scenario

log = logging.getLogger("traceguard.app")

UI_DIR = Path(__file__).parent / "ui"
# Strict CSP for the dashboard: only same-origin scripts/styles/fetches, no inline code, no framing.
UI_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
          "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _hard_exit() -> None:  # pragma: no cover - replaced in tests
    os._exit(1)


def create_app(settings: Optional[Settings] = None, extra_span_exporters=()) -> FastAPI:
    settings = settings or Settings.from_env()
    telemetry.configure_logging()
    provider = telemetry.init_tracing(settings, extra_span_exporters)
    tracer = provider.get_tracer("traceguard.workflow")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def sampler():  # feeds the dashboard's metric history from this process's own registry
            while True:
                _app.state.history.sample()
                await asyncio.sleep(settings.history_interval_s)
        task = asyncio.create_task(sampler())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="TraceGuard", version=settings.service_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.faults = FaultState()
    app.state.detector = IncidentDetector(settings)
    app.state.crash_fn: Callable[[], None] = _hard_exit
    app.state.started = True
    app.state.started_at = time.time()
    app.state.history = MetricsHistory(settings.history_max_points, settings.history_interval_s)
    app.state.api_limiter = RateLimiter(settings.rate_limit_api_per_min)
    app.state.run_limiter = RateLimiter(settings.rate_limit_runs_per_min)
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

    app.state.is_ready = is_ready
    app.state.is_healthy = lambda: faults.scenario is not Scenario.unhealthy

    pipeline = RecommendPipeline(tracer, settings)
    app.state.pipeline = pipeline
    app.state.tracer = tracer
    app.state.runner = SandboxRunner(pipeline, tracer, settings)
    app.state.history.sample()  # baseline sample so the first interval can already be computed

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
        # App-wide fault state (local development only; empty/"none" in public demo mode).
        outcome = await pipeline.run(req, faults, trace.get_current_span())
        result = JSONResponse(outcome.content, outcome.status_code)
        record_request_metric(outcome)
        detector.record(Sample(ts=time.time(), duration_ms=outcome.duration_ms, status_code=outcome.status_code,
                               correct=outcome.correct, trace_id=outcome.trace_id, scenario=outcome.scenario))
        run_detection()
        if outcome.trace_id:
            result.headers["X-Trace-Id"] = outcome.trace_id
        return result

    # -- fault injection (local development only) -----------------------------------------
    @app.get("/simulate-failure")
    def fault_status():
        return {"enabled": settings.fault_injection_allowed, "environment": settings.environment,
                **faults.as_dict()}

    @app.post("/simulate-failure")
    def simulate_failure(req: FaultRequest):
        if not settings.fault_injection_allowed:
            if settings.public_demo:
                raise HTTPException(403, "global fault injection does not exist in public demo mode; "
                                         "use the sandboxed scenarios on the dashboard")
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
                "jaeger_ui_url": None if settings.public_demo else settings.jaeger_ui_url}

    @app.get("/console", response_class=HTMLResponse)
    def console():
        return CONSOLE_HTML

    # -- unified dashboard (UI shell + JSON API) -------------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path == "/" or path.startswith("/ui"):
            response.headers["Content-Security-Policy"] = UI_CSP
            response.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(ui_api.router)
    shell = UI_DIR / "static" / "index.html"

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui/", status_code=307)

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def ui_home():
        return FileResponse(shell, media_type="text/html")

    @app.get("/ui/{section}", include_in_schema=False)
    def ui_section(section: str):
        if section not in ui_api.SECTIONS:
            raise HTTPException(404, "unknown section")
        return FileResponse(shell, media_type="text/html")

    app.mount("/ui/static", StaticFiles(directory=UI_DIR / "static"), name="ui-static")

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
