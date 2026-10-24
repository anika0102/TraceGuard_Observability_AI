"""JSON API behind the unified dashboard. Read-only, except for sandboxed demo runs.

Every payload says where its numbers come from:
  live          measured by this process right now (Prometheus registry, real spans, the incident detector)
  live_sandbox  produced by a demo run that just executed real requests, isolated from everything else
  recorded      replayed from a fixture captured in a verified local run (scenarios D and E)
There are no endpoints that run shell commands, kubectl or Ansible, and nothing here reads environment variables
other than a short allow-list of pod identity fields.
"""
from __future__ import annotations

import os
import platform
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import recordings, telemetry
from .config import Settings
from .ratelimit import RateLimiter, client_key
from .sandbox import CATALOG, SandboxBusy

SECTIONS = ["overview", "metrics", "traces", "incidents", "scenarios", "infrastructure"]
router = APIRouter(prefix="/api")

COMPONENT_BY_TYPE = {
    "latency": "recommend.lookup_catalog",
    "error_rate": "recommend.compute_recommendation",
    "correctness": "recommend.validate_business_rules",
    "readiness": "readiness probe (/ready)",
}


def _state(request: Request):
    return request.app.state


def _limit(kind: str):
    def dep(request: Request):
        st = _state(request)
        per_client: RateLimiter = st.run_limiter if kind == "run" else st.api_limiter
        overall: RateLimiter = st.run_global if kind == "run" else st.api_global
        ok, retry = per_client.allow(client_key(request, st.settings.client_ip_header))
        if ok:  # only charge the global ceiling for requests the per-client limit allowed
            ok, retry = overall.allow("*")
        if not ok:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(int(retry) + 1)})
    return dep


api_guard = Depends(_limit("api"))
run_guard = Depends(_limit("run"))


def _jaeger_url(settings: Settings) -> Optional[str]:
    return None if settings.public_demo else (settings.jaeger_ui_url or None)


def _rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def _slow_ms(settings: Settings) -> float:
    return settings.latency_p95_threshold_ms * 0.5  # a span this long is flagged as a slow operation


# --------------------------------------------------------------------------------------------------- meta
@router.get("/meta", dependencies=[api_guard])
def meta(request: Request):
    st = _state(request)
    s: Settings = st.settings
    return {
        "mode": "public" if s.public_demo else "local",
        "service": s.service_name, "version": s.service_version, "environment": s.environment,
        "fault_injection_global": s.fault_injection_allowed,
        "sections": SECTIONS,
        "links": {"jaeger": _jaeger_url(s), "prometheus_metrics": None if s.public_demo else "/metrics"},
        "limits": {"sandbox_max_requests": s.sandbox_max_requests, "sandbox_max_delay_ms": s.sandbox_max_delay_ms,
                   "runs_per_minute": s.rate_limit_runs_per_min},
        "thresholds": {"latency_p95_ms": s.latency_p95_threshold_ms, "error_rate": s.error_rate_threshold,
                       "incorrect_rate": s.correctness_failure_threshold},
        "labels": {"live": "Measured by this process", "live_sandbox": "Real requests in an isolated demo run",
                   "recorded": "Replayed from a verified local run", "not_available": "Not collected here"},
        "generated_at": round(time.time(), 3),
    }


# ----------------------------------------------------------------------------------------------- overview
@router.get("/overview", dependencies=[api_guard])
def overview(request: Request):
    st = _state(request)
    s: Settings = st.settings
    w = st.history.window_stats(300)
    active = st.detector.active()
    hist = st.detector.history()
    recs = recordings.load_all()
    uptime = time.time() - st.started_at
    return {
        "source": "live", "generated_at": round(time.time(), 3), "window": {"seconds": w["window_s"], "available": w["available"]},
        "service": {"ready": st.is_ready(), "healthy": st.is_healthy(), "uptime_s": round(uptime, 1),
                    "started_at": round(st.started_at, 3), "environment": s.environment,
                    "mode": "public" if s.public_demo else "local",
                    "scope": "this running instance (not a multi-region availability figure)"},
        "throughput": {"rps": w["rps"], "requests": w["requests"]},
        "latency": {"p50_ms": w["p50_ms"], "p95_ms": w["p95_ms"], "threshold_p95_ms": s.latency_p95_threshold_ms},
        "http_error_rate": {"value": w["error_rate"], "threshold": s.error_rate_threshold},
        "incorrect_outcome_rate": {"value": w["incorrect_rate"], "threshold": s.correctness_failure_threshold},
        "incidents": {"opened_by_detector": len(hist), "active": len(active),
                      "opened_by_demo_runs": len(st.runner.demo_incidents),
                      "note": "detector history is in memory and resets when the process restarts"},
        "alerts": {"active": [{"type": i.type, "severity": i.severity, "summary": i.summary,
                               "trace_id": i.trace_id} for i in active],
                   "kind": "Embedded detector alerts, judged over the detector's last "
                           f"{s.incident_window_size} requests (not Prometheus alert rules; the tiles above use the time window shown)"},
        "recovery": {"status": "recently restarted" if uptime < 300 else "stable since last start",
                     "uptime_s": round(uptime, 1),
                     "recorded_reference": {k: {"label": "RECORDED", **{x: v["summary"].get(x) for x in
                                                ("headline",)}} for k, v in recs.items()}},
        "notes": ([] if w["available"] and w["requests"] else
                  ["No requests in the window yet: throughput, latency and rates show 'no data', not zero."]),
    }


# ----------------------------------------------------------------------------------------------- metrics
@router.get("/metrics/history", dependencies=[api_guard])
def metrics_history(request: Request, window: int = Query(900, ge=60, le=3600)):
    st = _state(request)
    series = st.history.series(window_s=window)
    return {"source": "live", "window_s": window, **series, "stats": st.history.stats(),
            "thresholds": {"latency_p95_ms": st.settings.latency_p95_threshold_ms},
            "note": "Derived from this process's Prometheus counters/histograms every "
                    f"{st.history.interval_s:g}s; in memory, so history starts at the last process start."}


# ------------------------------------------------------------------------------------------------ traces
@router.get("/traces", dependencies=[api_guard])
def traces(request: Request, limit: int = Query(30, ge=1, le=100), errors_only: bool = False,
           origin: Optional[str] = Query(None, pattern="^(request|sandbox)$")):
    st = _state(request)
    store = telemetry.trace_store()
    return {"source": "live", "traces": store.list(limit=limit, errors_only=errors_only, origin=origin,
                                                   slow_ms=_slow_ms(st.settings)),
            "store": store.stats(), "slow_operation_threshold_ms": _slow_ms(st.settings),
            "jaeger_ui_url": _jaeger_url(st.settings)}


@router.get("/traces/{trace_id}", dependencies=[api_guard])
def trace_detail(request: Request, trace_id: str):
    st = _state(request)
    detail = telemetry.trace_store().get(trace_id.lower(), slow_ms=_slow_ms(st.settings))
    if detail is None:
        raise HTTPException(404, "trace not found (it may have been evicted: only the most recent are kept)")
    base = _jaeger_url(st.settings)
    return {"source": "live", **detail, "slow_operation_threshold_ms": _slow_ms(st.settings),
            "jaeger_url": f"{base}/trace/{trace_id.lower()}" if base else None}


# --------------------------------------------------------------------------------------------- incidents
def _component(inc: dict, store) -> dict:
    """Where the symptom was observed. From the trace when one exists; otherwise from the rule that fired."""
    fallback = COMPONENT_BY_TYPE.get(inc["type"], "unknown")
    tid = inc.get("trace_id")
    detail = store.get(tid) if tid else None
    if detail:
        spans = [x for x in detail["spans"] if not x["internal"] and not x["is_root"]]
        pick = None
        if inc["type"] == "latency":
            pick = max(spans, key=lambda x: x["duration_ms"], default=None)
        elif inc["type"] == "error_rate":
            pick = next((x for x in spans if x["status"] == "error"), None)
        elif inc["type"] == "correctness":
            pick = next((x for x in spans if x["attributes"].get("traceguard.outcome_correct") is False), None)
        if pick:
            return {"name": pick["name"], "basis": "trace"}
    return {"name": fallback, "basis": "rule"}


def _incident_view(inc: dict, origin: str, store) -> dict:
    detected = inc["detected_at"]
    resolved = inc.get("resolved_at")
    timeline = [{"t": detected, "event": "detected by rule", "detail": inc["summary"]}]
    if resolved:
        timeline.append({"t": resolved, "event": "condition cleared",
                         "detail": "the rule no longer fires for the current window; not a confirmed fix"})
    comp = _component(inc, store)
    return {**inc, "origin": inc.get("origin", origin), "affected_component": comp["name"],
            "affected_component_basis": comp["basis"], "timeline": timeline,
            "resolution": "resolved" if resolved else "open",
            "duration_s": round((resolved or time.time()) - detected, 1),
            "trace_link": f"/ui/traces?id={inc['trace_id']}" if inc.get("trace_id") else None}


@router.get("/incidents", dependencies=[api_guard])
def incidents(request: Request):
    st = _state(request)
    store = telemetry.trace_store()
    live = [_incident_view(i.as_dict(), "live", store) for i in st.detector.history()]
    demo = [_incident_view(d, "demo", store) for d in st.runner.demo_incidents]
    allv = sorted(live + demo, key=lambda x: x["detected_at"], reverse=True)
    return {"source": "live", "incidents": allv,
            "counts": {"open": sum(1 for i in allv if i["resolution"] == "open"), "total": len(allv),
                       "by_type": {t: sum(1 for i in allv if i["type"] == t) for t in COMPONENT_BY_TYPE}},
            "note": "Incidents are detected symptoms. A trace shows an example affected request and does not "
                    "prove root cause. 'demo' incidents come from sandboxed scenario runs; 'live' ones from "
                    "this instance's own traffic. Both reset when the process restarts."}


# ------------------------------------------------------------------------------------------------ scenarios
def _recording_summary(rec: Optional[dict]) -> Optional[dict]:
    return None if rec is None else {"recorded_at": rec["recorded_at"], "git_commit": rec["git_commit"],
                                     "headline": rec["summary"].get("headline")}


@router.get("/scenarios", dependencies=[api_guard])
def scenarios(request: Request):
    st = _state(request)
    recs = recordings.load_all()
    out = []
    for sid, spec in CATALOG.items():
        item = {"id": sid, "title": spec["title"], "description": spec["description"], "expected": spec["expected"],
                "kind": spec["kind"],
                "label": "LIVE_SANDBOX" if spec["kind"] == "live_sandbox" else "RECORDED",
                "runnable": spec["kind"] == "live_sandbox"}
        if spec["kind"] == "recorded":
            item["recording"] = _recording_summary(recs.get(sid))
            item["available"] = sid in recs
        out.append(item)
    s = st.settings
    return {"scenarios": out, "limits": {"max_requests": s.sandbox_max_requests, "max_delay_ms": s.sandbox_max_delay_ms,
                                         "runs_per_minute": s.rate_limit_runs_per_min},
            "safety": "Scenarios A-C run real requests through the real workflow with faults private to each "
                      "simulated request. D and E are recordings: nothing here can run kubectl, Ansible or shell."}


class RunRequest(BaseModel):
    requests: Optional[int] = Field(default=None, ge=1, le=100)
    delay_ms: Optional[int] = Field(default=None, ge=0, le=60000)


@router.post("/scenarios/{scenario_id}/run", dependencies=[run_guard])
async def run_scenario(request: Request, scenario_id: str, body: Optional[RunRequest] = None):
    st = _state(request)
    spec = CATALOG.get(scenario_id.upper())
    if spec is None:
        raise HTTPException(404, "unknown scenario")
    if spec["kind"] != "live_sandbox":
        raise HTTPException(409, "this scenario is a recording from a verified local run and cannot be executed here")
    body = body or RunRequest()
    try:
        return await st.runner.run(scenario_id.upper(), body.requests, body.delay_ms)
    except SandboxBusy as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "2"})


@router.get("/scenarios/{scenario_id}/recording", dependencies=[api_guard])
def scenario_recording(scenario_id: str):
    spec = CATALOG.get(scenario_id.upper())
    if spec is None:
        raise HTTPException(404, "unknown scenario")
    if spec["kind"] != "recorded":
        raise HTTPException(404, "this scenario runs live; there is no recording")
    rec = recordings.get(scenario_id.upper())
    if rec is None:
        raise HTTPException(404, "recording not available in this build")
    return rec


# ------------------------------------------------------------------------------------------ infrastructure
@router.get("/infrastructure", dependencies=[api_guard])
def infrastructure(request: Request):
    st = _state(request)
    s: Settings = st.settings
    store = telemetry.trace_store()
    # Only claim OUR Kubernetes deployment (its manifest injects POD_NAME). Hosting platforms such as Render run
    # containers on Kubernetes too and set KUBERNETES_SERVICE_HOST, which must not read as "the TraceGuard cluster".
    in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and bool(os.environ.get("POD_NAME"))
    pod = {k: os.environ.get(env) for k, env in (("pod_name", "POD_NAME"), ("namespace", "POD_NAMESPACE"))
           if os.environ.get(env)}
    recs = recordings.load_all()
    return {
        "live": {
            "source": "live",
            "service": {"name": s.service_name, "version": s.service_version, "environment": s.environment,
                        "mode": "public" if s.public_demo else "local", "python": platform.python_version(),
                        "started_at": round(st.started_at, 3), "uptime_s": round(time.time() - st.started_at, 1),
                        "memory_rss_mb": _rss_mb()},
            "probes": [{"name": "liveness", "path": "/health", "status": "ok" if st.is_healthy() else "failing"},
                       {"name": "readiness", "path": "/ready", "status": "ok" if st.is_ready() else "failing"}],
            "runtime": {"in_kubernetes": in_cluster, **pod,
                        "restart_count": None,
                        "restart_count_note": "A process cannot see its own container restart count; the "
                                              "recorded runs below show the real counts."},
            "telemetry": {"otlp_export_configured": bool(s.otlp_endpoint), "trace_store": store.stats(),
                          "metrics_history": st.history.stats(),
                          "tracing_excludes": "probes, /metrics and the dashboard's own API calls"},
        },
        "recorded": {
            "label": "RECORDED",
            "note": "Cluster behaviour (pod restarts, rollouts, Ansible) from verified local runs on a kind cluster.",
            "events": [{"scenario": k, "title": v["title"], "recorded_at": v["recorded_at"],
                        "git_commit": v["git_commit"], "headline": v["summary"].get("headline")}
                       for k, v in recs.items()],
        },
    }
