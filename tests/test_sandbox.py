"""Sandboxed scenarios A-C: real requests, isolated per request and per run, hard caps."""
import threading
import time

import pytest

from .conftest import REQ, build_client


def run(c, sid, **body):
    r = c.post(f"/api/scenarios/{sid}/run", json=body)
    return r


def test_scenario_a_is_healthy_and_raises_no_incident():
    c = build_client()
    d = run(c, "A").json()
    assert d["label"] == "LIVE_SANDBOX" and d["metrics"]["requests"] == 6
    assert all(r["status"] == 200 and r["correct"] is True for r in d["requests"])
    assert d["detection"]["incidents"] == [] and d["detection"]["matched"] is True


def test_scenario_b_is_slow_and_detected_but_runs_concurrently():
    c = build_client(slow_fault_delay_ms=1200)
    started = time.perf_counter()
    d = run(c, "B").json()
    wall = time.perf_counter() - started
    assert d["metrics"]["p95_ms"] >= 1200 * 0.9
    assert d["detection"]["detected"] == ["latency"] and d["detection"]["matched"] is True
    assert all(r["status"] == 200 for r in d["requests"])        # slow, not failing
    assert wall < 3.0, "6 requests with 1.2 s delay must overlap, not run one after another"


def test_scenario_c_is_http_200_everywhere_but_wrong_and_detected():
    c = build_client()
    d = run(c, "C").json()
    assert all(r["status"] == 200 for r in d["requests"]) and all(r["correct"] is False for r in d["requests"])
    assert d["metrics"]["incorrect_rate"] == 1.0 and d["metrics"]["error_rate"] == 0.0
    assert d["detection"]["detected"] == ["correctness"] and d["detection"]["matched"] is True
    assert c.get("/health").status_code == 200 and c.get("/ready").status_code == 200  # infra view stays green


def test_every_simulated_request_is_its_own_trace_and_traces_are_retrievable():
    c = build_client()
    d = run(c, "C").json()
    ids = [r["trace_id"] for r in d["requests"]]
    assert len(set(ids)) == len(ids) == 6
    for tid in ids:
        detail = c.get(f"/api/traces/{tid}").json()
        assert detail["summary"]["origin"] == "sandbox" and detail["summary"]["root_name"] == "sandbox.recommend"
        assert detail["summary"]["outcome_correct"] is False and detail["summary"]["fault_scenario"] == "wrong_result"
        attrs = [a for s in detail["spans"] for a in s["attributes"]]
        assert "traceguard.sandbox_run" in attrs


def test_the_app_wide_fault_state_and_detector_are_never_touched():
    c = build_client()
    run(c, "C"); run(c, "B", delay_ms=100)
    assert c.get("/simulate-failure").json()["scenario"] == "none"
    glob = c.get("/incidents").json()
    assert glob["window"]["samples"] == 0 and glob["history"] == [] and glob["active"] == []
    after = c.post("/recommend", json=REQ).json()
    assert after["fault_scenario"] == "none" and after["correctness"]["correct"] is True


def test_a_slow_run_does_not_slow_down_or_corrupt_anyone_elses_request():
    c = build_client(slow_fault_delay_ms=900)         # one app, two threads = two visitors
    holder = {}
    t = threading.Thread(target=lambda: holder.update(r=run(c, "B").json()))
    t.start()
    time.sleep(0.2)                                   # the slow demo run is now in flight
    began = time.perf_counter()
    other = c.post("/recommend", json=REQ).json()
    other_ms = (time.perf_counter() - began) * 1000
    t.join()
    assert other["fault_scenario"] == "none" and other["correctness"]["correct"] is True
    assert other_ms < 400, f"another visitor's request was slowed to {other_ms:.0f} ms"
    assert holder["r"]["metrics"]["p95_ms"] >= 900 * 0.9


def test_caps_clamp_requests_and_delay():
    c = build_client(sandbox_max_requests=4, sandbox_max_delay_ms=250, slow_fault_delay_ms=200)
    d = run(c, "B", requests=50, delay_ms=60000).json()
    assert len(d["requests"]) == 4 and d["parameters"]["injected_delay_ms"] == 250
    assert d["wall_ms"] < 2000


@pytest.mark.parametrize("body", [{"requests": 0}, {"requests": -3}, {"delay_ms": -1}, {"requests": 1000}, {"delay_ms": 10**9}])
def test_out_of_range_parameters_are_rejected(body):
    assert run(build_client(), "A", **body).status_code == 422


def test_concurrent_runs_are_capped():
    c = build_client(sandbox_max_concurrent_runs=1, slow_fault_delay_ms=800, rate_limit_runs_per_min=100)
    t = threading.Thread(target=lambda: run(c, "B"))
    t.start()
    time.sleep(0.25)
    busy = run(c, "A")
    t.join()
    assert busy.status_code == 429 and "Retry-After" in busy.headers
    assert run(c, "A").status_code == 200              # capacity is released afterwards


def test_recorded_and_unknown_scenarios_cannot_be_executed():
    c = build_client()
    for sid in ("D", "E", "d"):
        r = run(c, sid)
        assert r.status_code == 409 and "recording" in r.json()["detail"]
    assert run(c, "Z").status_code == 404 and run(c, "../etc").status_code == 404
    assert run(c, "c").status_code == 200              # ids are case-insensitive for runnable scenarios


def test_demo_incidents_are_listed_separately_from_live_ones_with_a_derived_component():
    c = build_client()
    run(c, "C")
    inc = c.get("/api/incidents").json()
    assert inc["counts"]["total"] == 1
    item = inc["incidents"][0]
    assert item["origin"] == "demo" and item["type"] == "correctness"
    assert item["affected_component"] == "recommend.validate_business_rules" and item["affected_component_basis"] == "trace"
    assert item["root_cause_confirmed"] is False and item["trace_link"].startswith("/ui/traces?id=")
    assert [e["event"] for e in item["timeline"]][0] == "detected by rule"


def test_sandbox_requests_are_real_telemetry_in_the_live_metrics():
    from app.metrics_history import read_registry
    c = build_client()
    before = read_registry()
    run(c, "C", requests=5)
    after = read_registry()
    assert after["requests"] - before["requests"] == 5 and after["incorrect"] - before["incorrect"] == 5
    assert after["buckets"][float("inf")] - before["buckets"][float("inf")] == 5


def test_visitor_ids_never_reach_telemetry_raw():
    c = build_client()
    d = run(c, "A", requests=2).json()
    blob = str(c.get(f"/api/traces/{d['requests'][0]['trace_id']}").json())
    assert "demo-" + d["run_id"] not in blob
