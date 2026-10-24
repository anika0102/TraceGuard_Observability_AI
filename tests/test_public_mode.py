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
    assert c.app.state.settings.fault_injection_allowed is False
    for scenario in ("slow", "error", "wrong_result", "not_ready", "unhealthy", "crash", "none"):
        assert c.post("/simulate-failure", json={"scenario": scenario}).status_code == 404   # route does not exist
    assert c.app.state.faults.scenario.value == "none"                                       # and nothing changed


def test_the_fault_handler_itself_still_refuses_if_the_route_were_ever_reachable():
    """Defence in depth: remove the 404 layer and the handler's own guard must still say no."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    from .conftest import make_settings
    app = create_app(make_settings(public_demo=True))
    app.user_middleware = [m for m in app.user_middleware if getattr(m.kwargs.get("dispatch"), "__name__", "") != "hide_operator_routes"]
    app.middleware_stack = None
    r = TestClient(app).post("/simulate-failure", json={"scenario": "slow"})
    assert r.status_code == 403 and "public demo" in r.json()["detail"]
    assert app.state.faults.scenario.value == "none"


@pytest.mark.parametrize("path", ["/metrics", "/incidents", "/console", "/simulate-failure", "/metrics/", "/docs", "/redoc", "/openapi.json"])
def test_operator_and_diagnostic_routes_do_not_exist_in_public_mode(path):
    c = public()
    assert c.get(path).status_code == 404
    body = c.get(path).text
    assert "prometheus" not in body.lower() and "traceguard_" not in body


@pytest.mark.parametrize("path", ["/metrics", "/incidents", "/console", "/simulate-failure", "/docs", "/openapi.json"])
def test_the_same_routes_still_work_in_local_mode(path):
    """Regression: hiding is public-mode only; the verified local tooling (Prometheus scrape etc.) is unchanged."""
    assert build_client().get(path).status_code == 200


def test_health_ready_and_the_dashboard_stay_available_in_public_mode():
    c = public()
    assert c.get("/health").status_code == 200 and c.get("/ready").status_code == 200
    assert c.get("/ui/").status_code == 200 and c.get("/api/overview").status_code == 200


def test_recommend_is_rate_limited_in_public_mode_only():
    pub = public(rate_limit_api_per_min=3)
    codes = [pub.post("/recommend", json=REQ).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = pub.post("/recommend", json=REQ)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and r.json()["error"] == "rate_limited"
    local = build_client(rate_limit_api_per_min=3)
    assert all(local.post("/recommend", json=REQ).status_code == 200 for _ in range(10))


def test_public_recommend_limit_cannot_be_dodged_with_forged_headers_by_default():
    c = public(rate_limit_api_per_min=2)
    for i in range(2):
        assert c.post("/recommend", json=REQ, headers={"True-Client-IP": f"198.51.100.{i}"}).status_code == 200
    assert c.post("/recommend", json=REQ, headers={"True-Client-IP": "198.51.100.99", "X-Forwarded-For": "203.0.113.5"}).status_code == 429


def test_recommend_is_always_healthy_in_public_mode():
    c = public()
    for _ in range(3):
        body = c.post("/recommend", json=REQ).json()
        assert body["fault_scenario"] == "none" and body["correctness"]["correct"] is True


def test_meta_hides_operator_links():
    d = public().get("/api/meta").json()
    assert d["mode"] == "public" and d["fault_injection_global"] is False
    assert d["links"] == {"jaeger": None, "prometheus_metrics": None}


def test_sandboxed_scenarios_still_work_in_public_mode():
    c = public()
    d = c.post("/api/scenarios/C/run", json={}).json()
    assert d["detection"]["matched"] is True and c.app.state.faults.scenario.value == "none"


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
                 "/api/infrastructure"):
        r = c.get(path)
        assert r.status_code == 200, path
        blobs.append(r.text)
    blobs += [c.get(p).text for p in ("/incidents", "/simulate-failure", "/metrics", "/console", "/health", "/ready")]
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
    assert s.public_demo is False and s.fault_injection_allowed is False and s.client_ip_header == ""
    assert Settings(public_demo=True, environment="local", enable_fault_injection=True).fault_injection_allowed is False


def test_client_ip_header_comes_only_from_the_environment(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    assert Settings.from_env().client_ip_header == ""
    monkeypatch.setenv("CLIENT_IP_HEADER", " True-Client-IP ")
    assert Settings.from_env().client_ip_header == "True-Client-IP"


@pytest.mark.parametrize("env,expected", [({"TRACEGUARD_PUBLIC_DEMO": "true"}, True), ({}, False), ({"TRACEGUARD_PUBLIC_DEMO": "no"}, False)])
def test_public_mode_is_enabled_from_the_environment(monkeypatch, env, expected):
    from app.config import Settings
    monkeypatch.delenv("TRACEGUARD_PUBLIC_DEMO", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert Settings.from_env().public_demo is expected
