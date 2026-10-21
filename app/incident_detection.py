"""Deterministic, offline incident detection over real request telemetry.

The detector consumes one `Sample` per /recommend request (latency, HTTP status, correctness
verdict, real trace ID) plus the current readiness state. It reports *symptoms*. A trace
attached to an incident is an example of an affected request, not proof of root cause.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Deque, Optional

from .config import Settings

LATENCY, ERROR_RATE, READINESS, CORRECTNESS = "latency", "error_rate", "readiness", "correctness"
INCIDENT_TYPES = (LATENCY, ERROR_RATE, READINESS, CORRECTNESS)

CAVEAT = (
    "Detected symptom only. The attached trace is an example of an affected request; "
    "root cause is not confirmed by this record."
)


@dataclass(frozen=True)
class Sample:
    ts: float
    duration_ms: float
    status_code: int
    correct: Optional[bool]  # None when no business check ran (e.g. the request failed)
    trace_id: Optional[str]
    scenario: str = "none"

    @property
    def is_error(self) -> bool:
        return self.status_code >= 500


@dataclass
class Incident:
    type: str
    severity: str
    summary: str
    evidence: dict
    trace_id: Optional[str]
    investigation_steps: list[str]
    recommended_remediation: str
    detected_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    root_cause_confirmed: bool = False
    caveat: str = CAVEAT

    def as_dict(self) -> dict:
        d = asdict(self)
        d["detected_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.detected_at))
        d["status"] = "resolved" if self.resolved_at else "open"
        return d


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[k - 1]


class IncidentDetector:
    def __init__(self, settings: Settings):
        self.s = settings
        self._samples: Deque[Sample] = deque(maxlen=settings.incident_window_size)
        self._active: dict[str, Incident] = {}
        self._history: Deque[Incident] = deque(maxlen=200)
        self._lock = threading.Lock()

    # -- input -------------------------------------------------------------------------
    def record(self, sample: Sample) -> None:
        with self._lock:
            self._samples.append(sample)

    def clear_window(self) -> None:
        with self._lock:
            self._samples.clear()

    # -- analysis ----------------------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            return self._stats(list(self._samples))

    @staticmethod
    def _stats(samples: list[Sample]) -> dict:
        n = len(samples)
        checked = [x for x in samples if x.correct is not None]
        durations = [x.duration_ms for x in samples]
        return {
            "samples": n,
            "error_rate": (sum(x.is_error for x in samples) / n) if n else 0.0,
            "p50_ms": round(percentile(durations, 50), 1),
            "p95_ms": round(percentile(durations, 95), 1),
            "checked": len(checked),
            "incorrect_rate": (sum(1 for x in checked if not x.correct) / len(checked)) if checked else 0.0,
        }

    def evaluate(self, ready: bool, now: Optional[float] = None) -> list[Incident]:
        """Apply the rules; returns incidents newly opened by this call."""
        now = time.time() if now is None else now
        with self._lock:
            samples = list(self._samples)
            findings = self._rules(samples, ready)
            opened: list[Incident] = []
            for itype in INCIDENT_TYPES:
                if itype in findings and itype not in self._active:
                    inc = findings[itype]
                    inc.detected_at = now
                    self._active[itype] = inc
                    self._history.append(inc)
                    opened.append(inc)
                elif itype in findings:
                    self._active[itype].evidence = findings[itype].evidence  # refresh numbers
                elif itype in self._active:
                    self._active.pop(itype).resolved_at = now
            return opened

    def _rules(self, samples: list[Sample], ready: bool) -> dict[str, Incident]:
        s, out = self.s, {}
        st = self._stats(samples)
        enough = st["samples"] >= s.incident_min_samples

        if not ready:
            out[READINESS] = Incident(
                READINESS, "critical", "Application reports not ready; Kubernetes removes it from Service endpoints.",
                {"ready": False},
                None,  # no request exists to point at: readiness probes are not traced
                ["kubectl -n traceguard describe pod -l app=traceguard   # probe failures, restarts, events",
                 "kubectl -n traceguard logs deploy/traceguard --tail=50",
                 "curl localhost:8000/ready   # check the reported reason"],
                "If the pod is merely not ready, fix the dependency; if it is stuck, run the Ansible "
                "recovery playbook (rollout restart).",
            )

        if enough and st["p95_ms"] > s.latency_p95_threshold_ms:
            slowest = max((x for x in samples if x.trace_id), key=lambda x: x.duration_ms, default=None)
            sev = "critical" if st["p95_ms"] > 3 * s.latency_p95_threshold_ms else "warning"
            out[LATENCY] = Incident(
                LATENCY, sev, f"p95 latency {st['p95_ms']} ms exceeds {s.latency_p95_threshold_ms} ms threshold.",
                {"p95_ms": st["p95_ms"], "p50_ms": st["p50_ms"], "threshold_ms": s.latency_p95_threshold_ms,
                 "samples": st["samples"]},
                slowest.trace_id if slowest else None,
                ["Open the attached trace in Jaeger and find the longest child span.",
                 "Compare span durations (validate_input / lookup_catalog / compute_recommendation) to a fast trace.",
                 "Check CPU throttling and downstream dependency latency."],
                "Identify the slow span first. A rollout restart only helps if the slowness is local to the pod.",
            )

        if enough and st["error_rate"] > s.error_rate_threshold:
            last_err = next((x for x in reversed(samples) if x.is_error and x.trace_id), None)
            sev = "critical" if st["error_rate"] > 0.5 else "warning"
            out[ERROR_RATE] = Incident(
                ERROR_RATE, sev,
                f"Error rate {st['error_rate']:.0%} exceeds {s.error_rate_threshold:.0%} threshold.",
                {"error_rate": round(st["error_rate"], 3), "threshold": s.error_rate_threshold,
                 "samples": st["samples"]},
                last_err.trace_id if last_err else None,
                ["Open the attached trace; look for spans with ERROR status and recorded exceptions.",
                 "Search logs for the same trace_id.",
                 "Check whether errors started after a deploy (kubectl rollout history)."],
                "If errors began after a deployment, roll back (Ansible: recovery_action=rollback); "
                "otherwise investigate the failing dependency.",
            )

        if st["checked"] and st["incorrect_rate"] > s.correctness_failure_threshold:
            last_bad = next((x for x in reversed(samples) if x.correct is False and x.trace_id), None)
            sev = "critical" if st["incorrect_rate"] >= 0.5 else "warning"
            out[CORRECTNESS] = Incident(
                CORRECTNESS, sev,
                f"{st['incorrect_rate']:.0%} of validated outcomes differ from the expected business result "
                "although HTTP responses succeeded.",
                {"incorrect_rate": round(st["incorrect_rate"], 3), "threshold": s.correctness_failure_threshold,
                 "checked": st["checked"]},
                last_bad.trace_id if last_bad else None,
                ["Open the attached trace; compare the validate_business_rules span attributes "
                 "(expected_sku vs actual_sku).",
                 "Check recent model/ranking/data changes: this class of failure is invisible to health checks.",
                 "Replay the same input against the previous release."],
                "Roll back the most recent model/config change (Ansible: recovery_action=rollback) "
                "after confirming the regression window.",
            )
        return out

    # -- output ------------------------------------------------------------------------
    def active(self) -> list[Incident]:
        with self._lock:
            return list(self._active.values())

    def history(self) -> list[Incident]:
        with self._lock:
            return list(self._history)
