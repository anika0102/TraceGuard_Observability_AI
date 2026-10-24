import subprocess
import sys

from prometheus_client.parser import text_string_to_metric_families

from .conftest import REQ


def test_counters_exist_at_zero_before_first_event():
    """Fresh interpreter: a counter that first appears non-zero makes Prometheus rate()/increase() miss the
    first burst, so the error/incorrect/incident series must be exported as 0 from startup."""
    code = (
        "from prometheus_client import REGISTRY\n"
        "from app import metrics\n"
        "get = REGISTRY.get_sample_value\n"
        "checks = {\n"
        " 'error': get('traceguard_recommend_requests_total', {'status': 'error', 'scenario': 'error'}),\n"
        " 'incorrect': get('traceguard_correctness_checks_total', {'result': 'incorrect'}),\n"
        " 'incident': get('traceguard_incidents_total', {'type': 'correctness', 'severity': 'critical'}),\n"
        "}\n"
        "print(checks)\n"
        "assert all(v == 0.0 for v in checks.values()), checks\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_not_ready_scenario_fails_readiness_but_not_liveness(client):
    client.post("/simulate-failure", json={"scenario": "not_ready"})
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200  # liveness unaffected: pod is removed, not restarted
    client.post("/simulate-failure", json={"scenario": "none"})
    assert client.get("/ready").status_code == 200


def test_unhealthy_scenario_fails_liveness(client):
    client.post("/simulate-failure", json={"scenario": "unhealthy"})
    assert client.get("/health").status_code == 503


def test_fault_injection_blocked_in_production(prod_client):
    r = prod_client.post("/simulate-failure", json={"scenario": "slow"})
    assert r.status_code == 403
    assert prod_client.get("/simulate-failure").json()["enabled"] is False


def test_fault_flag_alone_does_not_enable_in_production(prod_client):
    # prod_client sets enable_fault_injection=True with environment=production
    assert prod_client.post("/simulate-failure", json={"scenario": "error"}).status_code == 403
    assert prod_client.post("/recommend", json=REQ).json()["fault_scenario"] == "none"


def test_metrics_exposed_with_bounded_labels(client):
    client.post("/recommend", json={**REQ, "user_id": "secret-user-xyz"})
    text = client.get("/metrics").text
    assert "traceguard_http_requests_total" in text
    assert "traceguard_correctness_checks_total" in text
    assert "secret-user-xyz" not in text
    for fam in text_string_to_metric_families(text):
        for sample in fam.samples:
            assert "trace_id" not in sample.labels and "user_id" not in sample.labels
