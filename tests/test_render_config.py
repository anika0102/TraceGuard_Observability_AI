"""The Render Blueprint and Dockerfile: free-plan-only, public mode, no secrets, binds $PORT."""
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
SERVICE = BLUEPRINT["services"][0]
DOCKERFILE = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")


def test_exactly_one_free_docker_web_service():
    assert len(BLUEPRINT["services"]) == 1
    assert SERVICE["type"] == "web" and SERVICE["runtime"] == "docker"
    assert SERVICE["plan"] == "free", "plan must be explicit: Render defaults to a paid instance when omitted"


def test_no_paid_or_stateful_features_are_declared():
    paid_keys = {"disk", "disks", "numInstances", "scaling", "maxShutdownDelaySeconds", "preDeployCommand", "domains"}
    assert not (paid_keys & set(SERVICE)), f"paid/stateful keys present: {paid_keys & set(SERVICE)}"
    assert set(BLUEPRINT) == {"services"}, "no databases, key-value stores, env groups or other resources"


def test_automatic_deploys_are_off_to_protect_build_minutes():
    assert SERVICE["autoDeployTrigger"] == "off" and isinstance(SERVICE["autoDeployTrigger"], str)  # YAML 'off' alone is boolean False


def test_blueprint_points_at_files_that_exist():
    assert (ROOT / SERVICE["dockerfilePath"]).is_file() and SERVICE["dockerContext"] == "."


def test_health_check_path_is_a_public_mode_route_that_returns_200():
    from .conftest import build_client
    c = build_client(public_demo=True, environment="production")
    assert SERVICE["healthCheckPath"] == "/health" and c.get("/health").status_code == 200


def test_public_mode_environment_is_set_and_no_secret_is_committed():
    env = {e["key"]: e for e in SERVICE["envVars"]}
    assert env["TRACEGUARD_PUBLIC_DEMO"]["value"] == "true"
    assert env["TRACEGUARD_ENV"]["value"] == "production"
    assert env["CLIENT_IP_HEADER"]["value"] == "True-Client-IP"
    assert "TRACEGUARD_ENABLE_FAULT_INJECTION" not in env
    for e in env.values():
        assert "sync" not in e and "generateValue" not in e, "no secrets are needed or declared"
        assert not re.search(r"(?i)secret|token|password|key", e["key"].replace("TRACEGUARD", "")), e["key"]


def test_dockerfile_uses_python_312_and_runs_as_a_non_root_numeric_user():
    assert re.search(r"^FROM python:3\.12", DOCKERFILE, re.M)
    assert re.search(r"^USER 10001$", DOCKERFILE, re.M)


def test_dockerfile_binds_all_interfaces_on_the_port_render_provides():
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", DOCKERFILE, re.M).group(1))
    line = cmd[-1]
    assert "0.0.0.0" in line and "${PORT:-8000}" in line, "must bind 0.0.0.0 on $PORT, defaulting to 8000 for Compose/k8s"
    assert "--limit-concurrency" in line and "--no-server-header" in line and line.startswith("exec ")


@pytest.mark.skipif(shutil.which("uvicorn") is None, reason="uvicorn not on PATH")
def test_the_container_command_really_serves_the_public_demo_on_port(tmp_path):
    """Run the exact CMD from the Dockerfile with Render-style environment and check what a visitor could reach."""
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", DOCKERFILE, re.M).group(1))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PORT": str(port), "PYTHONPATH": str(ROOT), "TRACEGUARD_PUBLIC_DEMO": "true",
           "TRACEGUARD_ENV": "production", "CLIENT_IP_HEADER": "True-Client-IP"}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def get(path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers

    try:
        for _ in range(60):
            try:
                if get("/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.25)
        assert get("/health")[0] == 200 and get("/ui/")[0] == 200 and get("/api/overview")[0] == 200
        for hidden in ("/metrics", "/incidents", "/console", "/simulate-failure", "/docs", "/openapi.json"):
            assert get(hidden)[0] == 404, hidden
        assert "uvicorn" not in (get("/health")[1].get("server") or "").lower()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
