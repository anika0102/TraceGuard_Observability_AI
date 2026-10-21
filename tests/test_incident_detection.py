import time

from app.config import Settings
from app.incident_detection import CORRECTNESS, ERROR_RATE, LATENCY, READINESS, IncidentDetector, Sample, percentile

from .conftest import REQ

S = Settings(latency_p95_threshold_ms=1000, error_rate_threshold=0.2, incident_min_samples=5)


def sample(ms=50, status=200, correct=True, trace="a" * 32):
    return Sample(ts=time.time(), duration_ms=ms, status_code=status, correct=correct, trace_id=trace)


def types(incs):
    return {i.type for i in incs}


def test_percentile_nearest_rank():
    assert percentile([], 95) == 0.0
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10


def test_healthy_traffic_opens_nothing():
    d = IncidentDetector(S)
    for _ in range(10):
        d.record(sample())
    assert d.evaluate(ready=True) == []


def test_latency_incident_uses_slowest_real_trace():
    d = IncidentDetector(S)
    for i in range(10):
        d.record(sample(ms=2000 + i, trace=f"{i:032x}"))
    (inc,) = d.evaluate(ready=True)
    assert inc.type == LATENCY and inc.evidence["p95_ms"] > 1000
    assert inc.trace_id == f"{9:032x}"
    assert inc.root_cause_confirmed is False and inc.investigation_steps and inc.recommended_remediation


def test_latency_needs_minimum_samples():
    d = IncidentDetector(S)
    for _ in range(4):
        d.record(sample(ms=5000))
    assert d.evaluate(ready=True) == []


def test_error_rate_incident():
    d = IncidentDetector(S)
    for i in range(10):
        d.record(sample(status=500 if i % 2 else 200, correct=None if i % 2 else True, trace=f"{i:032x}"))
    (inc,) = d.evaluate(ready=True)
    assert inc.type == ERROR_RATE and inc.evidence["error_rate"] == 0.5
    assert inc.trace_id == f"{9:032x}"  # most recent failing request


def test_correctness_incident_despite_http_200():
    d = IncidentDetector(S)
    for _ in range(9):
        d.record(sample())
    d.record(sample(correct=False, trace="b" * 32))
    (inc,) = d.evaluate(ready=True)
    assert inc.type == CORRECTNESS and inc.trace_id == "b" * 32
    assert inc.evidence["incorrect_rate"] == 0.1


def test_correctness_incident_is_not_a_latency_or_error_incident():
    d = IncidentDetector(S)
    for _ in range(6):
        d.record(sample(correct=False))
    assert types(d.evaluate(ready=True)) == {CORRECTNESS}


def test_readiness_incident_has_no_invented_trace():
    d = IncidentDetector(S)
    (inc,) = d.evaluate(ready=False)
    assert inc.type == READINESS and inc.trace_id is None


def test_trace_id_is_none_when_samples_have_none():
    d = IncidentDetector(S)
    for _ in range(6):
        d.record(sample(ms=4000, trace=None))
    (inc,) = d.evaluate(ready=True)
    assert inc.trace_id is None


def test_incident_is_deduplicated_and_resolves():
    d = IncidentDetector(S)
    for _ in range(6):
        d.record(sample(correct=False))
    assert len(d.evaluate(ready=True)) == 1
    assert d.evaluate(ready=True) == []  # still open, not re-opened
    d.clear_window()
    d.evaluate(ready=True)
    assert d.active() == []
    assert d.history()[0].resolved_at is not None


def test_thresholds_are_configurable():
    d = IncidentDetector(Settings(latency_p95_threshold_ms=100, incident_min_samples=3))
    for _ in range(3):
        d.record(sample(ms=150))
    assert types(d.evaluate(ready=True)) == {LATENCY}


def test_http_flow_slow_then_incident_endpoint(client):
    client.post("/simulate-failure", json={"scenario": "slow", "delay_ms": 120})
    client.app.state.detector.s = Settings(latency_p95_threshold_ms=100, incident_min_samples=3)
    for _ in range(3):
        client.post("/recommend", json=REQ)
    data = client.get("/incidents").json()
    assert [i["type"] for i in data["active"]] == [LATENCY]
    assert data["active"][0]["trace_id"] and data["window"]["p95_ms"] >= 120


def test_http_flow_wrong_result_incident(client):
    client.post("/simulate-failure", json={"scenario": "wrong_result"})
    for _ in range(3):
        assert client.post("/recommend", json=REQ).status_code == 200
    data = client.get("/incidents").json()
    assert [i["type"] for i in data["active"]] == [CORRECTNESS]


def test_http_flow_not_ready_incident_without_traffic(client):
    client.post("/simulate-failure", json={"scenario": "not_ready"})
    client.get("/ready")
    assert [i["type"] for i in client.get("/incidents").json()["active"]] == [READINESS]


def test_console_served(client):
    assert "TraceGuard incident console" in client.get("/console").text
