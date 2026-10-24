"""Regression tests for scripts/port-forward.sh using a fake kubectl (no cluster needed).

Covers: forwards outlive the launching shell, PID tracking, idempotent start/stop/status, and that `stop`
never kills a process that is not one of our own port-forwards (unrelated forwards, reused PIDs).
Linux/WSL/macOS only: the script relies on bash, /dev/tcp and process command lines.
"""
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux /proc and bash")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "port-forward.sh"
SERVICES = ["traceguard", "jaeger", "prometheus", "grafana"]

FAKE_KUBECTL = textwrap.dedent(f"""\
    #!{sys.executable}
    import os, signal, socket, sys, time
    args = sys.argv[1:]
    if "port-forward" not in args:
        sys.exit(0)
    local = int(args[-1].split(":")[0])
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    if not os.environ.get("FAKE_NO_LISTEN"):
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", local)); s.listen(5)
    while True:
        time.sleep(1)
""")


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is not alive for our purposes
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


class Env:
    def __init__(self, tmp_path):
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "kubectl").write_text(FAKE_KUBECTL)
        (self.bin / "kubectl").chmod(0o755)
        self.pids = tmp_path / "pids"
        self.ports = dict(zip(SERVICES, free_ports(len(SERVICES))))
        self.extra = {}
        self.spawned = []

    def env(self):
        e = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "PF_PIDDIR": str(self.pids),
             "PF_SERVICES": " ".join(f"{s}:{p}:{p}" for s, p in self.ports.items()),
             "K8S_CONTEXT": "kind-test", "K8S_NAMESPACE": "tg-test"}
        e.update(self.extra)
        return e

    def run(self, cmd):
        return subprocess.run(["bash", str(SCRIPT), *cmd.split()], env=self.env(), capture_output=True, text=True,
                              timeout=60)

    def pid(self, svc):
        return int((self.pids / f"{svc}.pid").read_text().strip())

    def spawn_unrelated_forward(self, port):
        """A port-forward that is NOT ours: other context, other namespace, other service."""
        p = subprocess.Popen([str(self.bin / "kubectl"), "--context", "someone-elses-cluster", "-n", "other",
                              "port-forward", "svc/other-app", f"{port}:{port}"], env=self.env())
        self.spawned.append(p)
        assert wait_for(lambda: port_open(port))
        return p

    def cleanup(self):
        self.run("stop")
        for p in self.spawned:
            if p.poll() is None:
                p.send_signal(signal.SIGKILL)
                p.wait()


@pytest.fixture
def pf(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


def test_forwards_survive_the_launching_shell(pf):
    done = pf.run("start")  # this subprocess (the "one-shot session") has exited by the time we look
    assert done.returncode == 0, done.stdout + done.stderr
    for svc, port in pf.ports.items():
        pid = pf.pid(svc)
        assert alive(pid), f"{svc} died with the launching shell"
        assert port_open(port)
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
        assert "port-forward" in cmdline and f"svc/{svc}" in cmdline


def test_start_is_idempotent(pf):
    pf.run("start")
    before = {s: pf.pid(s) for s in SERVICES}
    again = pf.run("start")
    assert again.returncode == 0 and again.stdout.count("already running") == len(SERVICES)
    assert {s: pf.pid(s) for s in SERVICES} == before
    assert pf.run("status").returncode == 0


def test_stop_terminates_ours_and_is_idempotent(pf):
    pf.run("start")
    pids = [pf.pid(s) for s in SERVICES]
    assert pf.run("stop").returncode == 0
    assert wait_for(lambda: not any(alive(p) for p in pids))
    assert not any((pf.pids / f"{s}.pid").exists() for s in SERVICES)
    assert not any(port_open(p) for p in pf.ports.values())
    assert pf.run("stop").returncode == 0  # second stop is a no-op


def test_stop_never_touches_unrelated_port_forwards(pf):
    foreign_port = free_ports(1)[0]
    foreign = pf.spawn_unrelated_forward(foreign_port)
    pf.run("start")
    pf.run("stop")
    assert foreign.poll() is None and port_open(foreign_port), "stop killed an unrelated kubectl port-forward"


def test_stop_ignores_reused_pid(pf):
    innocent = subprocess.Popen(["sleep", "120"])
    pf.spawned.append(innocent)
    pf.pids.mkdir(parents=True, exist_ok=True)
    (pf.pids / "grafana.pid").write_text(str(innocent.pid))  # stale file pointing at an unrelated process
    out = pf.run("stop")
    assert out.returncode == 0 and "leaving it alone" in out.stdout
    assert innocent.poll() is None, "stop killed a process it did not start"
    assert not (pf.pids / "grafana.pid").exists()


def test_start_replaces_stale_pid_file(pf):
    innocent = subprocess.Popen(["sleep", "120"])
    pf.spawned.append(innocent)
    pf.pids.mkdir(parents=True, exist_ok=True)
    (pf.pids / "jaeger.pid").write_text(str(innocent.pid))
    assert pf.run("start").returncode == 0
    assert pf.pid("jaeger") != innocent.pid and port_open(pf.ports["jaeger"])
    assert innocent.poll() is None


def test_status_exit_codes(pf):
    assert pf.run("status").returncode == 1  # nothing running
    pf.run("start")
    ok = pf.run("status")
    assert ok.returncode == 0 and ok.stdout.count("accepting connections") == len(SERVICES)
    pf.run("stop")
    assert pf.run("status").returncode == 1


def test_status_detects_process_alive_but_port_dead(pf):
    pf.extra["FAKE_NO_LISTEN"] = "1"  # fake kubectl stays alive but never opens the port (like a dead tunnel)
    pf.ports = {"traceguard": pf.ports["traceguard"]}
    out = pf.run("start")  # start must report failure, not pretend success
    assert out.returncode != 0 and "FAILED" in out.stderr
    assert not (pf.pids / "traceguard.pid").exists()


def test_start_refuses_port_held_by_another_process(pf):
    pf.ports = {"traceguard": pf.ports["traceguard"]}
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", pf.ports["traceguard"]))
    blocker.listen(1)
    try:
        out = pf.run("start")
        assert out.returncode != 0 and "already in use" in out.stderr
        assert not (pf.pids / "traceguard.pid").exists()
    finally:
        blocker.close()


def test_usage_error_for_unknown_command(pf):
    assert pf.run("bogus").returncode == 2


def test_restart_single_service_leaves_the_others_untouched(pf):
    pf.run("start")
    before = {s: pf.pid(s) for s in SERVICES}
    out = pf.run("restart traceguard")
    assert out.returncode == 0, out.stdout + out.stderr
    after = {s: pf.pid(s) for s in SERVICES}
    assert after["traceguard"] != before["traceguard"] and not alive(before["traceguard"])
    assert {s: after[s] for s in SERVICES if s != "traceguard"} == {s: before[s] for s in SERVICES if s != "traceguard"}
    assert all(alive(p) and port_open(pf.ports[s]) for s, p in after.items())


def test_restart_single_service_starts_it_if_not_running(pf):
    out = pf.run("restart grafana")
    assert out.returncode == 0 and port_open(pf.ports["grafana"])
    assert not any((pf.pids / f"{s}.pid").exists() for s in SERVICES if s != "grafana")


def test_stop_single_service_is_idempotent_and_isolated(pf):
    pf.run("start")
    others = {s: pf.pid(s) for s in SERVICES if s != "jaeger"}
    victim = pf.pid("jaeger")
    assert pf.run("stop jaeger").returncode == 0
    assert wait_for(lambda: not alive(victim)) and not port_open(pf.ports["jaeger"])
    assert pf.run("stop jaeger").returncode == 0  # second call is a no-op
    assert all(alive(p) and port_open(pf.ports[s]) for s, p in others.items())


def test_status_single_service(pf):
    pf.run("start")
    pf.run("stop prometheus")
    assert pf.run("status traceguard").returncode == 0
    assert pf.run("status prometheus").returncode == 1
    assert pf.run("status").returncode == 1  # overall status still fails while one is down


@pytest.mark.parametrize("cmd", ["start", "stop", "restart", "status"])
def test_unknown_service_is_rejected_without_side_effects(pf, cmd):
    out = pf.run(f"{cmd} not-a-service")
    assert out.returncode == 2 and "unknown service" in out.stderr
    assert not pf.pids.exists() or not list(pf.pids.glob("*.pid"))
