"""Bounded in-memory store of real OpenTelemetry spans for the dashboard (no Jaeger needed).

Memory is bounded three ways: max traces (oldest evicted first), max spans per trace (extras are counted, not
kept) and max attribute value length. Only allow-listed attributes are stored, so request content, headers,
URLs, hostnames and exception messages can never reach the public API through this store.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import StatusCode

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_VALUE_LEN = 120
ALLOWED_ATTR_PREFIXES = ("traceguard.",)
ALLOWED_ATTR_EXACT = {"http.method", "http.route", "http.status_code"}
ALLOWED_EVENT_ATTRS = {"expected_sku", "actual_sku", "exception.type"}
# FastAPI/ASGI per-message spans: real, but noise in a waterfall, so they are flagged and hidden by default.
INTERNAL_SUFFIXES = (" http send", " http receive")


def _clean(value):
    if isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_VALUE_LEN]


def _safe_attrs(attrs) -> dict:
    out = {}
    for key, value in (attrs or {}).items():
        if key in ALLOWED_ATTR_EXACT or key.startswith(ALLOWED_ATTR_PREFIXES):
            out[key] = _clean(value)
    return out


@dataclass
class SpanRecord:
    span_id: str
    parent_id: Optional[str]
    name: str
    start_ns: int
    end_ns: int
    status: str                       # ok | error | unset
    attributes: dict
    events: list
    internal: bool

    @property
    def duration_ms(self) -> float:
        return round((self.end_ns - self.start_ns) / 1e6, 3)


@dataclass
class _Trace:
    spans: list = field(default_factory=list)
    dropped: int = 0


class TraceStore(SpanProcessor):
    def __init__(self, max_traces: int = 200, max_spans_per_trace: int = 100):
        self.max_traces = max_traces
        self.max_spans = max_spans_per_trace
        self._traces: "OrderedDict[str, _Trace]" = OrderedDict()
        self._lock = threading.Lock()

    # -- SpanProcessor -------------------------------------------------------------------
    def on_start(self, span, parent_context=None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        if span.end_time is None or span.start_time is None:
            return
        trace_id = format(span.context.trace_id, "032x")
        status = {StatusCode.ERROR: "error", StatusCode.OK: "ok"}.get(span.status.status_code, "unset")
        events = []
        for ev in span.events or ():
            attrs = {k: _clean(v) for k, v in (ev.attributes or {}).items() if k in ALLOWED_EVENT_ATTRS}
            events.append({"name": str(ev.name)[:MAX_VALUE_LEN], "attributes": attrs})
        rec = SpanRecord(
            span_id=format(span.context.span_id, "016x"),
            parent_id=format(span.parent.span_id, "016x") if span.parent else None,
            name=str(span.name)[:MAX_VALUE_LEN],
            start_ns=span.start_time, end_ns=span.end_time, status=status,
            attributes=_safe_attrs(span.attributes), events=events,
            internal=span.name.endswith(INTERNAL_SUFFIXES),
        )
        with self._lock:
            tr = self._traces.get(trace_id)
            if tr is None:
                tr = self._traces[trace_id] = _Trace()
                while len(self._traces) > self.max_traces:
                    self._traces.popitem(last=False)
            # The per-trace cap applies to child spans only: the root span ends last, and losing it would make
            # the whole trace unreadable, so a root is always kept (at most one extra span per trace).
            if len(tr.spans) >= self.max_spans and span.parent is not None:
                tr.dropped += 1
            else:
                tr.spans.append(rec)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    # -- queries -------------------------------------------------------------------------
    def clear(self) -> None:
        with self._lock:
            self._traces.clear()

    def stats(self) -> dict:
        with self._lock:
            return {"traces": len(self._traces), "max_traces": self.max_traces,
                    "spans": sum(len(t.spans) for t in self._traces.values()),
                    "max_spans_per_trace": self.max_spans}

    @staticmethod
    def _root(spans: list) -> SpanRecord:
        ids = {s.span_id for s in spans}
        roots = [s for s in spans if s.parent_id is None or s.parent_id not in ids]
        return min(roots or spans, key=lambda s: s.start_ns)

    def _summary(self, trace_id: str, tr: _Trace, slow_ms: float) -> dict:
        spans = tr.spans
        root = self._root(spans)
        scenario = next((s.attributes["traceguard.fault_scenario"] for s in spans
                         if "traceguard.fault_scenario" in s.attributes), None)
        correct = next((s.attributes["traceguard.outcome_correct"] for s in spans
                        if "traceguard.outcome_correct" in s.attributes), None)
        work = [s for s in spans if s is not root and not s.internal]
        slowest = max(work, key=lambda s: s.duration_ms, default=None)
        root_ms = root.duration_ms
        return {
            "trace_id": trace_id,
            "root_name": root.name,
            "origin": "sandbox" if root.name.startswith("sandbox.") else "request",
            "started_at": round(root.start_ns / 1e9, 3),
            "duration_ms": root_ms,
            "span_count": len(spans) + tr.dropped,
            "status": "error" if any(s.status == "error" for s in spans) else "ok",
            "fault_scenario": scenario,
            "outcome_correct": correct,
            "slowest_operation": None if slowest is None else {
                "name": slowest.name, "duration_ms": slowest.duration_ms,
                "share_pct": round(100 * slowest.duration_ms / root_ms, 1) if root_ms > 0 else None,
                "slow": slowest.duration_ms >= slow_ms},
        }

    def list(self, limit: int = 50, errors_only: bool = False, origin: Optional[str] = None,
             slow_ms: float = 500.0) -> list:
        with self._lock:
            items = list(self._traces.items())
        out = []
        for trace_id, tr in reversed(items):  # newest first
            if not tr.spans:
                continue
            s = self._summary(trace_id, tr, slow_ms)
            if errors_only and s["status"] != "error":
                continue
            if origin and s["origin"] != origin:
                continue
            out.append(s)
            if len(out) >= limit:
                break
        return out

    def get(self, trace_id: str, slow_ms: float = 500.0) -> Optional[dict]:
        if not TRACE_ID_RE.match(trace_id):
            return None
        with self._lock:
            tr = self._traces.get(trace_id)
            spans = list(tr.spans) if tr else []
            dropped = tr.dropped if tr else 0
        if not spans:
            return None
        root = self._root(spans)
        ordered = sorted(spans, key=lambda s: (s.start_ns, s.end_ns))
        by_id = {s.span_id: s for s in spans}

        def depth(s: SpanRecord) -> int:
            d, seen = 0, set()
            while s.parent_id in by_id and s.parent_id not in seen:
                seen.add(s.parent_id)
                s = by_id[s.parent_id]
                d += 1
            return d

        t0 = root.start_ns
        detail = {"summary": self._summary(trace_id, _Trace(spans=spans, dropped=dropped), slow_ms),
                  "dropped_spans": dropped, "spans": []}
        for s in ordered:
            detail["spans"].append({
                "span_id": s.span_id, "parent_id": s.parent_id, "name": s.name, "depth": depth(s),
                "offset_ms": round((s.start_ns - t0) / 1e6, 3), "duration_ms": s.duration_ms,
                "status": s.status, "internal": s.internal, "is_root": s is root,
                "slow": (s is not root) and not s.internal and s.duration_ms >= slow_ms,
                "attributes": s.attributes, "events": s.events})
        return detail
