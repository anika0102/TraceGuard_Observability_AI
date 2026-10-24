"""Public demo mode: no global fault injection, nothing infrastructure-changing, nothing internal exposed."""
import json

import pytest

from .conftest import REQ, build_client

CANARY = "canary-secret-9f3b"


def public(**kw):
    # environment=local + the flag ON on purpose: public mode must still refuse, whatever the config says
    return build_client(public_demo=True, environment="local", enable_fault_injection=True, **kw)


def test_global_fault_injection_is_impossible_even_when_every_flag_says_yes():
    c = public()
    for scenario in ("slow", "error", "wrong_result", "not_ready", "unhealthy", "crash", "none"):
        r = c.post("/simulate-failure", json={"scenario": scenario})
        assert r.status_code == 403 and "public demo" in r.json()["detail"]
    status = c.get("/simulate-failure").json()
    assert status["enabled"] is False and status["scenario"] == "none"


def test_recommend_is_always_healthy_in_public_mode():
    c = public()
    for _ in range(3):
        body = c.post("/recommend", json=REQ).json()
        assert body["fault_scenario"] == "none" and body["correctness"]["correct"] is True


def test_meta_hides_operator_links():
    d = public().get("/api/meta").json()
    assert d["mode"] == "public" and d["fault_injection_global"] is False
    assert d["links"] == {"jaeger": None, "prometheus_metrics": None}
    assert public().get("/incidents").json()["jaeger_ui_url"] is None


def test_sandboxed_scenarios_still_work_in_public_mode():
    c = public()
    d = c.post("/api/scenarios/C/run", json={}).json()
    assert d["detection"]["matched"] is True and c.get("/simulate-failure").json()["scenario"] == "none"


def test_trace_detail_has_no_jaeger_link_in_public_mode():
    c = public()
    tid = c.post("/recommend", json=REQ).json()["trace_id"]
    assert c.get(f"/api/traces/{tid}").json()["jaeger_url"] is None
    assert c.get("/api/traces").json()["jaeger_ui_url"] is None


def test_no_endpoint_can_run_commands_or_change_infrastructure():
    app = public().app
    posts = sorted(r.path for r in app.routes if "POST" in getattr(r, "methods", ()))
    assert posts == ["/api/scenarios/{scenario_id}/run", "/recommend", "/simulate-failure"]
    forbidden = ("exec", "kubectl", "ansible", "shell", "deploy", "restart", "delete", "rollout", "cmd", "command")
    for route in app.routes:
        path = getattr(route, "path", "").lower()
        assert not any(word in path for word in forbidden), f"suspicious route {path}"


def test_no_environment_variable_or_internal_address_is_ever_exposed(monkeypatch):
    monkeypatch.setenv("TRACEGUARD_SECRET_CANARY", CANARY)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("HOSTNAME", "internal-host-77")
    monkeypatch.setenv("POD_NAME", "traceguard-abc")
    c = public(otlp_endpoint="collector.internal.example:4317")
    c.post("/api/scenarios/C/run", json={})
    blobs = []
    for path in ("/api/meta", "/api/overview", "/api/metrics/history", "/api/traces", "/api/incidents", "/api/scenarios",
                 "/api/infrastructure", "/incidents", "/simulate-failure"):
        r = c.get(path)
        assert r.status_code == 200, path
        blobs.append(r.text)
    text = "\n".join(blobs)
    for secret in (CANARY, "10.96.0.1", "internal-host-77", "collector.internal.example"):
        assert secret not in text, f"leaked {secret}"
    infra = json.loads(blobs[6])["live"]
    assert infra["runtime"]["in_kubernetes"] is True and infra["runtime"]["pod_name"] == "traceguard-abc"   # the allow-list only
    assert infra["telemetry"]["otlp_export_configured"] is True


def test_dashboard_pages_and_assets_work_in_public_mode():
    c = public()
    assert c.get("/ui/scenarios").status_code == 200 and c.get("/ui/static/js/main.js").status_code == 200


def test_production_defaults_are_the_safe_ones():
    from app.config import Settings
    s = Settings()
    assert s.public_demo is False and s.fault_injection_allowed is False and s.trust_proxy_headers is False
    assert Settings(public_demo=True, environment="local", enable_fault_injection=True).fault_injection_allowed is False


@pytest.mark.parametrize("env,expected", [({"TRACEGUARD_PUBLIC_DEMO": "true"}, True), ({}, False), ({"TRACEGUARD_PUBLIC_DEMO": "no"}, False)])
def test_public_mode_is_enabled_from_the_environment(monkeypatch, env, expected):
    from app.config import Settings
    monkeypatch.delenv("TRACEGUARD_PUBLIC_DEMO", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert Settings.from_env().public_demo is expected
