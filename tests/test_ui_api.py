"""Dashboard JSON API: real values, honest 'no data', source labels, safe error handling."""
import json

import pytest

from .conftest import REQ, build_client


def test_meta_describes_mode_limits_and_labels():
    d = build_client().get("/api/meta").json()
    assert d["mode"] == "local" and d["sections"] == ["overview", "metrics", "traces", "incidents", "scenarios", "infrastructure"]
    assert set(d["labels"]) == {"live", "live_sandbox", "recorded", "not_available"}
    assert d["links"]["prometheus_metrics"] == "/metrics" and d["limits"]["sandbox_max_requests"] == 8


def test_overview_with_no_traffic_says_no_data_instead_of_zero():
    c = build_client()
    d = c.get("/api/overview").json()
    assert d["source"] == "live" and d["window"]["available"] is False
    assert d["throughput"]["rps"] is None and d["latency"]["p95_ms"] is None
    assert d["http_error_rate"]["value"] is None and d["incorrect_outcome_rate"]["value"] is None
    assert d["service"]["ready"] is True and d["incidents"]["active"] == 0
    assert any("No requests" in n for n in d["notes"])


def test_overview_reflects_real_traffic():
    c = build_client()
    c.app.state.history.sample()
    for _ in range(10):
        assert c.post("/recommend", json=REQ).status_code == 200
    c.app.state.history.sample()
    d = c.get("/api/overview").json()
    assert d["window"]["available"] is True and d["throughput"]["requests"] == 10 and d["throughput"]["rps"] > 0
    assert d["http_error_rate"]["value"] == 0.0 and d["incorrect_outcome_rate"]["value"] == 0.0
    assert 0 < d["latency"]["p95_ms"] < 1000


def test_overview_surfaces_wrong_results_and_errors_from_real_requests():
    c = build_client()
    c.app.state.history.sample()
    c.post("/simulate-failure", json={"scenario": "wrong_result"})
    for _ in range(4):
        c.post("/recommend", json=REQ)
    c.post("/simulate-failure", json={"scenario": "error", "error_every": 2})
    for _ in range(4):
        c.post("/recommend", json=REQ)
    c.app.state.history.sample()
    d = c.get("/api/overview").json()
    # 4 wrong results; then 4 requests under error_every=2: 2 returned 500 (no business check), 2 succeeded correctly
    assert d["incorrect_outcome_rate"]["value"] == round(4 / 6, 4)     # 4 of the 6 validated outcomes were wrong
    assert d["http_error_rate"]["value"] == 0.25                       # 2 of 8 requests returned 500
    assert d["incidents"]["active"] >= 1 and {a["type"] for a in d["alerts"]["active"]} >= {"correctness"}


def test_metrics_history_shape_and_window_validation():
    c = build_client()
    c.app.state.history.sample()
    c.post("/recommend", json=REQ)
    c.app.state.history.sample()
    d = c.get("/api/metrics/history?window=900").json()
    # create_app takes a baseline sample at startup, so: baseline + the two samples above
    assert d["source"] == "live" and len(d["points"]) == 3 and {"t", "rps", "p50_ms", "p95_ms", "error_rate", "incorrect_rate", "ready"} <= set(d["points"][0])
    assert c.get("/api/metrics/history?window=10").status_code == 422
    assert c.get("/api/metrics/history?window=99999").status_code == 422


def test_trace_list_and_detail_show_the_real_workflow_spans():
    c = build_client()
    tid = c.post("/recommend", json=REQ).json()["trace_id"]
    listing = c.get("/api/traces").json()
    assert listing["traces"][0]["trace_id"] == tid and listing["traces"][0]["origin"] == "request"
    detail = c.get(f"/api/traces/{tid}").json()
    names = [s["name"] for s in detail["spans"] if not s["internal"]]
    for expected in ("recommend.validate_input", "recommend.lookup_catalog", "recommend.compute_recommendation",
                     "recommend.validate_business_rules", "recommend.build_response"):
        assert expected in names
    depths = {s["name"]: s["depth"] for s in detail["spans"]}
    assert depths["recommend.lookup_catalog"] == depths["recommend.validate_input"] > 0


def test_slow_operation_is_identified():
    c = build_client()
    c.post("/simulate-failure", json={"scenario": "slow", "delay_ms": 650})
    tid = c.post("/recommend", json=REQ).json()["trace_id"]
    detail = c.get(f"/api/traces/{tid}").json()
    slow = [s["name"] for s in detail["spans"] if s["slow"]]
    assert slow == ["recommend.lookup_catalog"]
    assert detail["summary"]["slowest_operation"]["name"] == "recommend.lookup_catalog"
    assert detail["summary"]["slowest_operation"]["share_pct"] > 90


@pytest.mark.parametrize("tid", ["nothex", "../../etc/passwd", "g" * 32, "a" * 31, "0" * 32])
def test_bad_or_unknown_trace_ids_are_a_clean_404(tid):
    r = build_client().get(f"/api/traces/{tid}")
    assert r.status_code == 404 and "Traceback" not in r.text


def test_incident_view_for_a_live_incident_has_component_timeline_and_caveat():
    c = build_client()
    c.post("/simulate-failure", json={"scenario": "wrong_result"})
    for _ in range(4):
        c.post("/recommend", json=REQ)
    inc = c.get("/api/incidents").json()["incidents"][0]
    assert inc["origin"] == "live" and inc["type"] == "correctness" and inc["resolution"] == "open"
    assert inc["affected_component"] == "recommend.validate_business_rules" and inc["affected_component_basis"] == "trace"
    assert inc["root_cause_confirmed"] is False and "symptom" in inc["caveat"].lower()
    assert inc["investigation_steps"] and inc["recommended_remediation"] and inc["timeline"][0]["event"] == "detected by rule"
    c.post("/simulate-failure", json={"scenario": "none", "clear_incident_window": True})
    c.get("/incidents")                                   # evaluating again resolves the rule
    again = c.get("/api/incidents").json()["incidents"][0]
    assert again["resolution"] == "resolved" and [e["event"] for e in again["timeline"]][-1] == "condition cleared"


def test_readiness_incident_falls_back_to_the_rule_when_there_is_no_trace():
    c = build_client()
    c.post("/simulate-failure", json={"scenario": "not_ready"})
    c.get("/ready")
    inc = c.get("/api/incidents").json()["incidents"][0]
    assert inc["type"] == "readiness" and inc["trace_link"] is None and inc["affected_component_basis"] == "rule"


def test_scenario_catalogue_labels_d_and_e_as_recorded_and_not_runnable():
    d = build_client().get("/api/scenarios").json()
    by_id = {s["id"]: s for s in d["scenarios"]}
    assert list(by_id) == ["A", "B", "C", "D", "E"]
    assert all(by_id[i]["runnable"] and by_id[i]["label"] == "LIVE_SANDBOX" for i in "ABC")
    assert all(not by_id[i]["runnable"] and by_id[i]["label"] == "RECORDED" for i in "DE")
    assert "kubectl" in d["safety"] and "recordings" in d["safety"]


def test_infrastructure_is_honest_about_what_a_process_cannot_know():
    d = build_client().get("/api/infrastructure").json()
    live = d["live"]
    assert live["source"] == "live" and {p["name"] for p in live["probes"]} == {"liveness", "readiness"}
    assert all(p["status"] == "ok" for p in live["probes"])
    assert live["runtime"]["restart_count"] is None and "recorded runs" in live["runtime"]["restart_count_note"]
    assert d["recorded"]["label"] == "RECORDED"


def test_probe_status_follows_the_real_state():
    c = build_client()
    c.post("/simulate-failure", json={"scenario": "unhealthy"})
    probes = {p["name"]: p["status"] for p in c.get("/api/infrastructure").json()["live"]["probes"]}
    assert probes == {"liveness": "failing", "readiness": "ok"}
    c.post("/simulate-failure", json={"scenario": "not_ready"})
    probes = {p["name"]: p["status"] for p in c.get("/api/infrastructure").json()["live"]["probes"]}
    assert probes == {"liveness": "ok", "readiness": "failing"}


def test_dashboard_polling_is_not_traced_and_api_responses_are_not_cached():
    from app import telemetry
    c = build_client()
    for path in ("/api/overview", "/api/metrics/history", "/api/traces", "/api/incidents", "/api/scenarios", "/ui/", "/", "/ui/metrics", "/incidents", "/console"):
        c.get(path, follow_redirects=False)
    assert telemetry.trace_store().stats()["traces"] == 0
    r = c.get("/api/overview")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_error_responses_never_leak_internals():
    c = build_client()
    for r in (c.get("/api/scenarios/Z/recording"), c.get("/api/scenarios/A/recording"), c.post("/api/scenarios/Z/run", json={})):
        assert r.status_code in (404, 409) and "Traceback" not in r.text and "/app" not in r.text and ".py" not in r.text
    json.loads(c.get("/api/overview").text)  # always valid JSON
