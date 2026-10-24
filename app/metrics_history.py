"""Bounded history of real Prometheus-registry values, sampled in-process, for dashboard charts.

Every number is derived from counter / histogram deltas of metrics this process really exposes. If no request
happened in a window the derived value is `None` (shown as a gap / "no traffic"), never 0.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable, Optional

from . import metrics

INF = float("inf")


def _sum_total(counter, label: str = None) -> dict:
    """Sum of `<name>_total` samples, optionally grouped by one label."""
    out: dict = {}
    for fam in counter.collect():
        for s in fam.samples:
            if s.name.endswith("_total"):
                key = s.labels.get(label, "") if label else ""
                out[key] = out.get(key, 0.0) + s.value
    return out


def read_registry() -> dict:
    """One snapshot of the metrics the dashboard needs (cheap: a few dozen series)."""
    by_status = _sum_total(metrics.RECOMMEND_REQUESTS, "status")
    by_result = _sum_total(metrics.CORRECTNESS, "result")
    buckets: dict = {}
    count = 0.0
    for fam in metrics.RECOMMEND_DURATION.collect():
        for s in fam.samples:
            if s.name.endswith("_bucket"):
                le = INF if s.labels["le"] == "+Inf" else float(s.labels["le"])
                buckets[le] = buckets.get(le, 0.0) + s.value
            elif s.name.endswith("_count"):
                count += s.value
    ready = next((s.value for f in metrics.READY.collect() for s in f.samples), None)
    active = sum(s.value for f in metrics.ACTIVE_INCIDENTS.collect() for s in f.samples)
    return {
        "requests": sum(by_status.values()),
        "errors": by_status.get("error", 0.0),
        "correct": by_result.get("correct", 0.0),
        "incorrect": by_result.get("incorrect", 0.0),
        "buckets": buckets, "duration_count": count,
        "incidents_opened": sum(_sum_total(metrics.INCIDENTS).values()),
        "ready": ready, "active_incidents": active,
    }


def quantile_ms(q: float, start_buckets: dict, end_buckets: dict) -> Optional[float]:
    """histogram_quantile over the bucket deltas between two cumulative snapshots; None without observations."""
    bounds = sorted(end_buckets)
    cum = []
    for b in bounds:
        d = end_buckets[b] - start_buckets.get(b, 0.0)
        if d < 0:
            return None
        cum.append(d)
    total = cum[-1] if cum else 0.0
    if total <= 0:
        return None
    target = q * total
    prev_bound, prev_cum = 0.0, 0.0
    for bound, c in zip(bounds, cum):
        if c >= target:
            if math.isinf(bound):
                return round(prev_bound * 1000, 2)
            if c == prev_cum:
                return round(bound * 1000, 2)
            frac = (target - prev_cum) / (c - prev_cum)
            return round((prev_bound + (bound - prev_bound) * frac) * 1000, 2)
        prev_bound, prev_cum = bound, c
    return None


def _derive(a: dict, b: dict, dt: float) -> dict:
    """Rates and quantiles between snapshot a (older) and b (newer)."""
    d_req = b["requests"] - a["requests"]
    d_err = b["errors"] - a["errors"]
    d_ok, d_bad = b["correct"] - a["correct"], b["incorrect"] - a["incorrect"]
    checked = d_ok + d_bad
    valid = d_req >= 0 and d_err >= 0 and checked >= 0 and dt > 0
    return {
        "rps": round(d_req / dt, 4) if valid else None,
        "requests": d_req if valid else None,
        "error_rate": round(d_err / d_req, 4) if valid and d_req > 0 else None,
        "incorrect_rate": round(d_bad / checked, 4) if valid and checked > 0 else None,
        "p50_ms": quantile_ms(0.5, a["buckets"], b["buckets"]) if valid else None,
        "p95_ms": quantile_ms(0.95, a["buckets"], b["buckets"]) if valid else None,
    }


class MetricsHistory:
    def __init__(self, max_points: int = 720, interval_s: float = 5.0, clock: Callable[[], float] = time.time,
                 reader: Callable[[], dict] = read_registry):
        self.max_points = max_points
        self.interval_s = interval_s
        self._clock = clock
        self._reader = reader
        self._samples: deque = deque(maxlen=max_points)
        self.started_at = clock()

    def sample(self) -> dict:
        snap = {"t": self._clock(), **self._reader()}
        self._samples.append(snap)
        return snap

    def __len__(self) -> int:
        return len(self._samples)

    def series(self, window_s: float = 900, rate_window_s: float = 60) -> dict:
        samples = list(self._samples)
        if not samples:
            return {"points": [], "interval_s": self.interval_s, "rate_window_s": rate_window_s}
        cutoff = samples[-1]["t"] - window_s
        points = []
        for i, cur in enumerate(samples):
            if cur["t"] < cutoff:
                continue
            base = next((s for s in samples[:i] if s["t"] >= cur["t"] - rate_window_s), None)
            derived = _derive(base, cur, cur["t"] - base["t"]) if base is not None else {
                k: None for k in ("rps", "requests", "error_rate", "incorrect_rate", "p50_ms", "p95_ms")}
            points.append({"t": round(cur["t"], 3), **derived, "ready": cur["ready"],
                           "active_incidents": cur["active_incidents"]})
        return {"points": points, "interval_s": self.interval_s, "rate_window_s": rate_window_s}

    def window_stats(self, window_s: float = 300) -> dict:
        """Aggregate over the last `window_s` seconds (or since start, if shorter). Needs two samples."""
        samples = list(self._samples)
        empty = {"available": False, "window_s": None, "rps": None, "requests": None, "error_rate": None,
                 "incorrect_rate": None, "p50_ms": None, "p95_ms": None}
        if len(samples) < 2:
            return empty
        last = samples[-1]
        first = next((s for s in samples if s["t"] >= last["t"] - window_s), samples[0])
        if first is last:
            first = samples[-2]
        dt = last["t"] - first["t"]
        return {"available": True, "window_s": round(dt, 1), **_derive(first, last, dt)}

    def stats(self) -> dict:
        return {"points": len(self._samples), "max_points": self.max_points, "interval_s": self.interval_s}
