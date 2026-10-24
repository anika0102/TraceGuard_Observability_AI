from .conftest import REQ

EXPECTED_CHILD_SPANS = {
    "recommend.validate_input", "recommend.lookup_catalog", "recommend.compute_recommendation",
    "recommend.validate_business_rules", "recommend.build_response",
}


def test_recommend_returns_best_item_within_budget(client):
    r = client.post("/recommend", json={**REQ, "budget": 100})
    body = r.json()
    assert r.status_code == 200
    assert body["recommendation"]["sku"] == "E-101"  # best in-stock electronics <= 100
    assert body["correctness"]["correct"] is True
    assert r.headers["X-Trace-Id"] == body["trace_id"]


def test_out_of_stock_item_never_recommended(client):
    body = client.post("/recommend", json={"user_id": "u", "category": "books", "budget": 500}).json()
    assert body["recommendation"]["sku"] == "B-201"  # B-203 scores higher but is out of stock


def test_no_candidates_is_valid_and_correct(client):
    body = client.post("/recommend", json={**REQ, "budget": 5}).json()
    assert body["recommendation"] is None and body["correctness"]["correct"] is True


def test_unknown_category_returns_400(client):
    r = client.post("/recommend", json={**REQ, "category": "weapons"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_input"


def test_schema_validation_returns_422(client):
    assert client.post("/recommend", json={**REQ, "budget": -1}).status_code == 422


def test_request_produces_correlated_spans(client, spans):
    body = client.post("/recommend", json=REQ).json()
    finished = spans.get_finished_spans()
    names = {s.name for s in finished}
    assert EXPECTED_CHILD_SPANS <= names
    tid = int(body["trace_id"], 16)
    assert {s.context.trace_id for s in finished if s.name in EXPECTED_CHILD_SPANS} == {tid}
    server = [s for s in finished if s.parent is None]
    assert len(server) == 1 and server[0].attributes["traceguard.fault_scenario"] == "none"
    child = next(s for s in finished if s.name == "recommend.lookup_catalog")
    assert child.attributes["traceguard.operation"] == "lookup_catalog"
    assert child.attributes["traceguard.status"] == "ok"
    assert "traceguard.duration_ms" in child.attributes


def test_user_id_not_stored_raw_in_spans(client, spans):
    client.post("/recommend", json={**REQ, "user_id": "alice@example.com"})
    for s in spans.get_finished_spans():
        assert "alice@example.com" not in str(dict(s.attributes))


def test_slow_scenario_adds_latency_to_span(client, spans):
    delay_ms = 300
    # asyncio timers can fire slightly early relative to perf_counter/span timestamps (about one OS timer
    # tick, ~15.6 ms on Windows), so require 90% of the delay. A request without the fault takes ~1-8 ms,
    # so this still fails by a wide margin if the delay is not applied.
    min_observed_ms = delay_ms * 0.9
    client.post("/simulate-failure", json={"scenario": "slow", "delay_ms": delay_ms})
    body = client.post("/recommend", json=REQ).json()
    assert body["latency_ms"] >= min_observed_ms and body["fault_scenario"] == "slow"
    lookup = next(s for s in spans.get_finished_spans() if s.name == "recommend.lookup_catalog")
    assert (lookup.end_time - lookup.start_time) / 1e6 >= min_observed_ms
    assert lookup.attributes["traceguard.injected_delay_ms"] == delay_ms  # exact configured value


def test_error_scenario_returns_500_and_error_span(client, spans):
    client.post("/simulate-failure", json={"scenario": "error"})
    r = client.post("/recommend", json=REQ)
    assert r.status_code == 500 and r.json()["trace_id"]
    bad = next(s for s in spans.get_finished_spans() if s.name == "recommend.compute_recommendation")
    assert bad.status.status_code.name == "ERROR"
    assert any(e.name == "exception" for e in bad.events)


def test_error_every_nth(client):
    client.post("/simulate-failure", json={"scenario": "error", "error_every": 2})
    codes = [client.post("/recommend", json=REQ).status_code for _ in range(4)]
    assert codes == [200, 500, 200, 500]


def test_crash_scenario_calls_crash_fn_not_in_production(client, prod_client):
    called = []
    client.app.state.crash_fn = lambda: called.append(1)
    client.post("/simulate-failure", json={"scenario": "crash"})
    import time
    time.sleep(1.3)
    assert called == [1]
    assert prod_client.post("/simulate-failure", json={"scenario": "crash"}).status_code == 403
