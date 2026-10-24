"""Guardrail tests for ansible/recover-deployment.yml, run against a fake kubectl that logs every call.

They prove that (1) unauthorized contexts/namespaces/deployments/actions are refused before ANY kubectl call,
(2) extra-vars cannot widen the allow-list or replace the command/probe, and (3) every kubectl call the playbook
does make is pinned to --context kind-traceguard --namespace traceguard on deployment/traceguard.

Needs ansible-playbook (pip install ansible-core in a venv, Linux/WSL2/macOS). Skipped when it is not installed.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "ansible" / "recover-deployment.yml"
INVENTORY = ROOT / "ansible" / "inventory.ini"


def find_ansible():
    found = shutil.which("ansible-playbook")
    if found:
        return found
    candidate = Path.home() / ".venvs" / "ansible" / "bin" / "ansible-playbook"
    return str(candidate) if candidate.exists() else None


ANSIBLE = find_ansible()
pytestmark = pytest.mark.skipif(
    ANSIBLE is None or not sys.platform.startswith(("linux", "darwin")),
    reason="ansible-playbook not installed (pip install ansible-core) or unsupported platform",
)

FAKE_KUBECTL = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:2] == ["version", "--client"]:
        print("Client Version: v1.30.2")
    elif "get" in args and "json" in args:
        print(json.dumps({{"status": {{"readyReplicas": 1}}, "spec": {{"replicas": 1, "template": {{"spec": {{
            "containers": [{{"image": "traceguard:0.1.0"}}]}}}}}}}}))
    elif "exec" in args:
        print(json.dumps({{"ready": "ready", "http_status": 200, "correct": True, "sku": "E-100", "trace_id": "abc"}}))
    elif "history" in args:
        print("deployment.apps/traceguard\\nREVISION  CHANGE-CAUSE")
        for i in range(1, int(os.environ.get("FAKE_REVISIONS", "2")) + 1):
            print(f"{{i}}         <none>")
    else:
        print("ok")
""")

PINNED = ["--context", "kind-traceguard", "--namespace", "traceguard"]


class Run:
    def __init__(self, tmp_path):
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "kubectl").write_text(FAKE_KUBECTL)
        (self.bin / "kubectl").chmod(0o755)
        self.log = tmp_path / "kubectl.log"
        self.home = tmp_path / "home"
        self.home.mkdir()

    def play(self, *args, check=False, revisions=2):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_LOG": str(self.log),
               "FAKE_REVISIONS": str(revisions), "ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0",
               "ANSIBLE_HOME": str(self.home)}
        cmd = [ANSIBLE, "-i", str(INVENTORY), str(PLAYBOOK), *(["--check"] if check else []), *args]
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        return done.returncode, done.stdout + done.stderr

    @property
    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def run(tmp_path):
    return Run(tmp_path)


def assert_all_pinned(calls):
    for argv in calls:
        if argv[:2] == ["version", "--client"]:
            continue
        assert argv[:4] == PINNED, f"kubectl call not pinned to kind-traceguard/traceguard: {argv}"


def test_real_run_only_touches_the_pinned_deployment(run):
    rc, out = run.play()
    assert rc == 0, out
    assert "RESULT: SUCCESS" in out
    assert_all_pinned(run.calls)
    mutating = [c for c in run.calls if "restart" in c or "undo" in c]
    assert mutating == [[*PINNED, "rollout", "restart", "deployment/traceguard"]]


def test_dry_run_makes_no_mutating_call(run):
    rc, out = run.play(check=True)
    assert rc == 0 and "DRY RUN" in out, out
    assert_all_pinned(run.calls)
    assert not [c for c in run.calls if "restart" in c or "undo" in c]


def test_rollback_uses_undo_on_the_pinned_deployment(run):
    rc, out = run.play("-e", "recovery_action=rollback")
    assert rc == 0, out
    assert_all_pinned(run.calls)
    assert [*PINNED, "rollout", "undo", "deployment/traceguard"] in run.calls


@pytest.mark.parametrize("extra", [
    ["-e", "recovery_action=delete"],
    ["-e", "k8s_namespace=kube-system"],
    ["-e", "k8s_deployment=coredns"],
    ["-e", "k8s_context=docker-desktop"],
    ["-e", "k8s_context=minikube"],
    ["-e", "k8s_context=kind-other"],
    ["-e", "rollout_timeout=1s;--all"],
    ["-e", "rollout_timeout=1000000s"],
])
def test_unauthorized_requests_are_refused_before_any_kubectl_call(run, extra):
    rc, out = run.play(*extra)
    assert rc != 0 and "Refusing to run" in out, out
    assert run.calls == [], f"kubectl was invoked despite the refusal: {run.calls}"


@pytest.mark.parametrize("extra", [
    # attempts to widen the allow-list the way the OLD playbook allowed (variables), in several encodings
    ["-e", "k8s_context=attacker", "-e", '{"allowed_contexts":["attacker"]}'],
    ["-e", "k8s_namespace=kube-system", "-e", '{"allowed_namespaces":["kube-system"]}'],
    ["-e", "k8s_deployment=coredns", "-e", '{"allowed_deployments":["coredns"]}'],
    ["-e", "recovery_action=delete", "-e", '{"allowed_actions":["delete"]}'],
    ["-e", '{"k8s_context":"attacker","allowed_contexts":["attacker","kind-traceguard"]}'],
])
def test_extra_vars_cannot_widen_the_allow_list(run, extra):
    rc, out = run.play(*extra)
    assert rc != 0 and "Refusing to run" in out, out
    assert run.calls == []


def test_extra_vars_cannot_replace_the_kubectl_command(run):
    """The old playbook built every command from a `kubectl` variable, which -e could replace."""
    rc, out = run.play("-e", '{"kubectl":["kubectl","--context","evil","--namespace","kube-system"]}')
    assert rc == 0, out
    assert_all_pinned(run.calls)
    assert not any("evil" in c or "kube-system" in c for call in run.calls for c in call)


def test_extra_vars_cannot_inject_code_into_the_pod(run):
    """The old playbook executed a `probe_py` variable inside the application pod."""
    rc, out = run.play("-e", "probe_py=print('INJECTED')")
    assert rc == 0, out
    execs = [c for c in run.calls if "exec" in c]
    assert execs, "expected the probe to run"
    assert not any("INJECTED" in part for call in execs for part in call)
    assert all("http://localhost:8000" in call[-1] for call in execs)  # the literal probe.py content


def test_rollback_without_previous_revision_fails_with_clean_message(run):
    rc, out = run.play("-e", "recovery_action=rollback", check=True, revisions=1)
    assert rc != 0
    assert "RESULT: FAILURE during recovery action 'rollback'" in out
    assert "No previous revision to roll back to" in out
    assert not [c for c in run.calls if "undo" in c]
    assert ".." not in out.split("RESULT: FAILURE", 1)[1].split("\n", 1)[0], "double punctuation in failure summary"
