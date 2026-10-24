"""Bounded, allow-listed in-memory span store behind the dashboard's trace views."""
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode

from app.trace_store import MAX_VALUE_LEN, TraceStore


def make(max_traces=3, max_spans=4):
    store = TraceStore(max_traces=max_traces, max_spans_per_trace=max_spans)
    provider = TracerProvider()
    provider.add_span_processor(store)
    return store, provider.get_tracer("test")


def one_trace(tracer, name="root", children=("a", "b"), **root_attrs):
    with tracer.start_as_current_span(name, attributes=root_attrs) as root:
        for c in children:
            with tracer.start_as_current_span(c):
                pass
        return format(root.get_span_context().trace_id, "032x")


def test_oldest_traces_are_evicted_beyond_the_cap():
    store, tracer = make(max_traces=3)
    ids = [one_trace(tracer, f"t{i}") for i in range(5)]
    assert store.stats()["traces"] == 3
    assert store.get(ids[0]) is None and store.get(ids[1]) is None
    assert store.get(ids[4]) is not None


def test_child_spans_are_capped_but_the_root_is_always_kept():
    store, tracer = make(max_spans=4)
    tid = one_trace(tracer, "root", children=[f"c{i}" for i in range(12)])
    detail = store.get(tid)
    names = [s["name"] for s in detail["spans"]]
    assert "root" in names and any(s["is_root"] for s in detail["spans"])
    assert len([n for n in names if n != "root"]) == 4
    assert detail["dropped_spans"] == 8
    assert detail["summary"]["span_count"] == 13  # kept + dropped is still reported honestly


def test_only_allow_listed_attributes_are_stored_and_values_are_truncated():
    store, tracer = make()
    tid = one_trace(tracer, "root", **{
        "traceguard.category": "books", "http.method": "POST", "traceguard.long": "x" * 500,
        "http.url": "http://internal.example:8000/recommend?token=abc", "user.email": "a@b.example",
        "http.request.header.authorization": "Bearer secret", "net.peer.ip": "10.1.2.3"})
    attrs = store.get(tid)["spans"][0]["attributes"]
    assert set(attrs) == {"traceguard.category", "http.method", "traceguard.long"}
    assert len(attrs["traceguard.long"]) == MAX_VALUE_LEN
    blob = str(store.get(tid))
    for leaked in ("secret", "internal.example", "a@b.example", "10.1.2.3"):
        assert leaked not in blob


def test_exception_events_keep_the_type_but_not_the_message():
    store, tracer = make()
    with tracer.start_as_current_span("root") as span:
        span.record_exception(RuntimeError("password=hunter2 failed for host db.internal"))
        span.set_status(Status(StatusCode.ERROR, "boom"))
        tid = format(span.get_span_context().trace_id, "032x")
    detail = store.get(tid)
    ev = detail["spans"][0]["events"][0]
    assert ev["name"] == "exception" and ev["attributes"] == {"exception.type": "RuntimeError"}
    assert "hunter2" not in str(detail) and detail["summary"]["status"] == "error"


def test_summary_finds_the_slowest_operation_and_flags_slow_ones():
    import time
    store, tracer = make()
    with tracer.start_as_current_span("root") as root:
        with tracer.start_as_current_span("fast"):
            pass
        with tracer.start_as_current_span("slowpoke"):
            time.sleep(0.06)
        tid = format(root.get_span_context().trace_id, "032x")
    summary = store.list(slow_ms=50)[0]
    assert summary["trace_id"] == tid
    slowest = summary["slowest_operation"]
    assert slowest["name"] == "slowpoke" and slowest["slow"] is True and slowest["share_pct"] > 50
    spans = {s["name"]: s for s in store.get(tid, slow_ms=50)["spans"]}
    assert spans["slowpoke"]["slow"] is True and spans["fast"]["slow"] is False and spans["root"]["slow"] is False


def test_framework_spans_are_flagged_internal_and_origin_is_derived():
    store, tracer = make()
    tid = one_trace(tracer, "sandbox.recommend", children=["POST /x http send", "work"])
    spans = {s["name"]: s for s in store.get(tid)["spans"]}
    assert spans["POST /x http send"]["internal"] is True and spans["work"]["internal"] is False
    assert store.get(tid)["summary"]["origin"] == "sandbox"
    assert store.list(origin="request") == [] and len(store.list(origin="sandbox")) == 1


def test_errors_only_filter():
    store, tracer = make()
    one_trace(tracer, "ok")
    with tracer.start_as_current_span("bad") as sp:
        sp.set_status(Status(StatusCode.ERROR, "x"))
    assert [t["root_name"] for t in store.list(errors_only=True)] == ["bad"]


@pytest.mark.parametrize("bad_id", ["", "../../etc/passwd", "ABCDEF" * 5 + "AB", "g" * 32, "a" * 31, "a" * 33, "a" * 32 + "/x"])
def test_malformed_trace_ids_are_rejected_without_lookup(bad_id):
    store, tracer = make()
    one_trace(tracer)
    assert store.get(bad_id) is None
