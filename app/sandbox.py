"""Sandboxed demo scenarios A-C: real requests through the real pipeline, isolated per request and per run.

Isolation guarantees (all covered by tests):
  * every simulated request gets its OWN FaultState; the app-wide fault state is never read or written
  * every simulated request is its own root trace (not a child of the HTTP call that triggered the run)
  * every run gets its OWN IncidentDetector; the app-wide detector and its incident history are untouched
  * hard caps: requests, injected delay, concurrent runs (and the API adds a per-client rate limit)
Scenarios D and E (Kubernetes pod failure, Ansible recovery) are NOT runnable here; they exist only as
recordings from verified local runs (see recordings.py).
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from dataclasses import replace
from typing import Optional

from opentelemetry.context import Context

from . import pipeline as pl
from .config import Settings
from .faults import FaultState
from .incident_detection import IncidentDetector, Sample, percentile
from .schemas import RecommendRequest, Scenario

DEFAULT_REQUESTS = 6

# Rotating valid inputs; each has at least two in-stock candidates so wrong_result always shows a mismatch.
INPUTS = [("electronics", 250.0), ("books", 100.0), ("home", 150.0)]

CATALOG = {
    "A": {
        "id": "A", "title": "Healthy request", "kind": "live_sandbox", "fault": Scenario.none,
        "description": "Normal operation: valid requests through the full traced workflow.",
        "expected": "Every request returns HTTP 200 with the expected business result; no incident is raised.",
        "expected_incident": None,
    },
    "B": {
        "id": "B", "title": "Slow response", "kind": "live_sandbox", "fault": Scenario.slow,
        "description": "A controlled delay is injected into the catalog lookup for the requests of this run only.",
        "expected": "Requests succeed but slowly; p95 latency exceeds the threshold and a latency incident opens.",
        "expected_incident": "latency",
    },
    "C": {
        "id": "C", "title": "Incorrect outcome with HTTP 200", "kind": "live_sandbox", "fault": Scenario.wrong_result,
        "description": "A stale ranking serves the runner-up product: the response is technically successful.",
        "expected": "Every response is HTTP 200 and probes stay green, but the outcome validator flags the wrong "
                    "result and a correctness incident opens.",
        "expected_incident": "correctness",
    },
    "D": {
        "id": "D", "title": "Kubernetes pod failure", "kind": "recorded",
        "description": "A container crash (kubelet restarts the container) and a pod deletion (ReplicaSet "
                       "recreates the pod) on a local kind cluster.",
        "expected": "Kubernetes restores the pod within seconds; readiness gates traffic; in-memory app state resets.",
    },
    "E": {
        "id": "E", "title": "Controlled Ansible recovery", "kind": "recorded",
        "description": "An operator-run Ansible playbook restarts the deployment (and can roll it back), then "
                       "verifies readiness and sends a real test request.",
        "expected": "Rolling restart with no downtime, a correct post-recovery result, and a clear SUCCESS summary.",
    },
}


class SandboxBusy(Exception):
    pass


class SandboxRunner:
    def __init__(self, pipeline: pl.RecommendPipeline, tracer, settings: Settings):
        self.pipeline = pipeline
        self.tracer = tracer
        self.settings = settings
        self._inflight = 0
        self._lock = threading.Lock()
        self.demo_incidents: deque = deque(maxlen=settings.demo_incident_history)

    def _acquire(self) -> None:
        with self._lock:
            if self._inflight >= self.settings.sandbox_max_concurrent_runs:
                raise SandboxBusy("too many demo runs in progress; try again in a moment")
            self._inflight += 1

    def _release(self) -> None:
        with self._lock:
            self._inflight -= 1

    def _faults_for(self, scenario_id: str, delay_ms: int) -> FaultState:
        fs = FaultState()  # private to ONE simulated request
        fault = CATALOG[scenario_id]["fault"]
        if fault is not Scenario.none:
            fs.set(fault, delay_ms=delay_ms if fault is Scenario.slow else 0)
        return fs

    async def _one(self, index: int, run_id: str, scenario_id: str, delay_ms: int) -> dict:
        category, budget = INPUTS[index % len(INPUTS)]
        req = RecommendRequest(user_id=f"demo-{run_id}", category=category, budget=budget)
        faults = self._faults_for(scenario_id, delay_ms)
        # context=Context() makes this a fresh ROOT span, so each simulated request is its own trace.
        with self.tracer.start_as_current_span(
                "sandbox.recommend", context=Context(),
                attributes={"traceguard.sandbox_run": run_id, "traceguard.sandbox_scenario": scenario_id}) as root:
            outcome = await self.pipeline.run(req, faults, root)
        pl.record_request_metric(outcome)
        content = outcome.content
        return {"index": index, "status": outcome.status_code, "latency_ms": round(outcome.duration_ms, 2),
                "correct": outcome.correct, "trace_id": outcome.trace_id,
                "sku": (content.get("recommendation") or {}).get("sku") if outcome.status_code == 200 else None,
                "input": {"category": category, "budget": budget},
                "_sample": Sample(ts=time.time(), duration_ms=outcome.duration_ms, status_code=outcome.status_code,
                                  correct=outcome.correct, trace_id=outcome.trace_id, scenario=outcome.scenario)}

    async def run(self, scenario_id: str, count: Optional[int] = None, delay_ms: Optional[int] = None) -> dict:
        spec = CATALOG.get(scenario_id)
        if spec is None or spec["kind"] != "live_sandbox":
            raise KeyError(scenario_id)
        s = self.settings
        n = DEFAULT_REQUESTS if count is None else max(1, min(int(count), s.sandbox_max_requests))
        delay = s.slow_fault_delay_ms if delay_ms is None else max(0, min(int(delay_ms), s.sandbox_max_delay_ms))
        delay = min(delay, s.sandbox_max_delay_ms)
        run_id = uuid.uuid4().hex[:8]

        self._acquire()
        started = time.perf_counter()
        try:
            rows = await asyncio.gather(*(self._one(i, run_id, scenario_id, delay) for i in range(n)))
        finally:
            self._release()
        wall_ms = round((time.perf_counter() - started) * 1000, 1)

        # Isolated detector: only this run's samples, never the app-wide one.
        detector = IncidentDetector(replace(s, incident_min_samples=min(s.incident_min_samples, n)))
        for row in rows:
            detector.record(row["_sample"])
        opened = detector.evaluate(ready=True)
        incidents = []
        for inc in opened:
            d = inc.as_dict()
            d.update({"origin": "demo", "run_id": run_id, "scenario": scenario_id})
            incidents.append(d)
            self.demo_incidents.append(d)

        durations = [r["latency_ms"] for r in rows]
        errors = sum(1 for r in rows if r["status"] >= 500)
        checked = [r for r in rows if r["correct"] is not None]
        incorrect = sum(1 for r in checked if not r["correct"])
        for r in rows:
            r.pop("_sample")
        found = sorted(i["type"] for i in incidents)
        expected = [spec["expected_incident"]] if spec["expected_incident"] else []
        return {
            "scenario": scenario_id, "title": spec["title"], "label": "LIVE_SANDBOX", "run_id": run_id,
            "description": spec["description"], "expected": spec["expected"],
            "observed": self._observed(scenario_id, rows, durations, incorrect, found),
            "parameters": {"requests": n, "injected_delay_ms": delay if scenario_id == "B" else 0,
                           "caps": {"max_requests": s.sandbox_max_requests, "max_delay_ms": s.sandbox_max_delay_ms}},
            "requests": rows,
            "metrics": {"requests": n, "p50_ms": round(percentile(durations, 50), 1),
                        "p95_ms": round(percentile(durations, 95), 1), "error_rate": round(errors / n, 3),
                        "incorrect_rate": round(incorrect / len(checked), 3) if checked else None},
            "detection": {"incidents": incidents, "detected": sorted(found), "expected": expected,
                          "matched": sorted(found) == sorted(expected)},
            "isolation": {"app_wide_fault_state_used": False, "app_wide_incident_detector_used": False,
                          "each_request_has_private_fault_state": True, "each_request_is_its_own_trace": True},
            "wall_ms": wall_ms,
        }

    @staticmethod
    def _observed(scenario_id: str, rows: list, durations: list, incorrect: int, found: list) -> str:
        n = len(rows)
        ok200 = sum(1 for r in rows if r["status"] == 200)
        parts = [f"{ok200}/{n} requests returned HTTP 200", f"{incorrect}/{n} outcomes were incorrect",
                 f"p95 latency {round(percentile(durations, 95), 1)} ms"]
        parts.append("incident detected: " + ", ".join(found) if found else "no incident detected")
        return "; ".join(parts) + "."
