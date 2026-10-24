#!/usr/bin/env python3
"""Record scenarios D (pod failure) and E (Ansible recovery) from REAL runs on the local kind cluster.

The dashboard replays these fixtures (clearly labelled RECORDED) because a public deployment has no cluster.
Nothing here is invented: every timeline entry is a state change seen by a read-only observer (kubectl get pods,
Prometheus `up`), or an action this script performed, with its measured time.

Requirements: the kind cluster from scripts/deploy.sh, port-forwards running (scripts/port-forward.sh start),
ansible-playbook on PATH (scenario E). It only touches context kind-traceguard, namespace traceguard.

    python3 scripts/record_scenarios.py            # records D and E into app/ui/recordings/
    python3 scripts/record_scenarios.py D          # only D
"""
import argparse
import datetime
import json
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CTX, NS = "kind-traceguard", "traceguard"
K = ["kubectl", "--context", CTX, "-n", NS]
BASE, PROM = "http://127.0.0.1:8000", "http://127.0.0.1:9090"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, **kw)


def http(method, path, body=None, base=BASE, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def prom(expr):
    try:
        url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        res = json.load(urllib.request.urlopen(url, timeout=3))["data"]["result"]
        return res[0]["value"][1] if res else None
    except Exception:
        return None


def app_start_time():
    try:
        with urllib.request.urlopen(BASE + "/metrics", timeout=2) as r:
            for line in r.read().decode().splitlines():
                if line.startswith("process_start_time_seconds"):
                    return float(line.split()[1])
    except Exception:
        pass
    return None


class Observer(threading.Thread):
    """Read-only: snapshots pod state and Prometheus `up` twice a second."""

    def __init__(self):
        super().__init__(daemon=True)
        self.t0 = time.time()
        self.snaps = []     # (t, {pod: state}, up)
        self.marks = []     # (t, text)
        self._stop = threading.Event()

    def mark(self, text):
        self.marks.append((time.time() - self.t0, text))
        return self.marks[-1][0]

    def pods(self):
        out = sh(K + ["get", "pods", "-l", "app=traceguard", "-o", "json"], timeout=8).stdout
        state = {}
        for p in json.loads(out)["items"]:
            cs = (p["status"].get("containerStatuses") or [{}])[0]
            st = cs.get("state", {})
            kind = next(iter(st), "unknown")
            detail = kind
            if kind == "terminated":
                detail = f"terminated (exit {st['terminated'].get('exitCode')})"
            elif kind == "waiting":
                detail = f"waiting ({st['waiting'].get('reason')})"
            ready = next((c["status"] == "True" for c in p["status"].get("conditions", []) if c["type"] == "Ready"), False)
            state[p["metadata"]["name"]] = {"container": detail, "restarts": cs.get("restartCount", 0), "ready": ready,
                                            "terminating": bool(p["metadata"].get("deletionTimestamp"))}
        return state

    def run(self):
        while not self._stop.is_set():
            try:
                self.snaps.append((time.time() - self.t0, self.pods(), prom('up{job="traceguard"}')))
            except Exception:
                pass
            time.sleep(0.5)

    def stop(self):
        self._stop.set()
        self.join(timeout=5)

    def events(self, since=0.0):
        """Human-readable state changes derived from consecutive snapshots."""
        out, prev_pods, prev_up = [], {}, None
        for t, pods, up in self.snaps:
            if t >= since:
                for name, s in pods.items():
                    old = prev_pods.get(name)
                    if old is None:
                        out.append((t, "pod", f"pod {name} appeared ({s['container']})"))
                        continue
                    if s["container"] != old["container"]:
                        out.append((t, "pod", f"pod {name}: container {s['container']}"))
                    if s["restarts"] != old["restarts"]:
                        out.append((t, "pod", f"pod {name}: restart count {old['restarts']} -> {s['restarts']}"))
                    if s["ready"] != old["ready"]:
                        out.append((t, "pod", f"pod {name} {'became Ready' if s['ready'] else 'is no longer Ready'}"))
                    if s["terminating"] and not old["terminating"]:
                        out.append((t, "pod", f"pod {name} is terminating"))
                for name in prev_pods.keys() - pods.keys():
                    out.append((t, "pod", f"pod {name} removed"))
                if up is not None and up != prev_up and prev_up is not None:
                    out.append((t, "prometheus", f"Prometheus target up: {prev_up} -> {up}"))
            prev_pods, prev_up = pods, up if up is not None else prev_up
        return out

    def first(self, pattern, after):
        for t, _cat, text in self.events():
            if t >= after and re.search(pattern, text):
                return t
        return None

    def no_ready_pod_seconds(self, start, end):
        """Longest stretch with no Ready pod inside [start, end]: from the first snapshot with none Ready to the
        first snapshot where one is Ready again (or to `end`). Resolution is the polling period (about 0.5-1 s)."""
        best, began = 0.0, None
        for t, pods, _ in self.snaps:
            if not (start <= t <= end):
                continue
            ready = any(p["ready"] and not p["terminating"] for p in pods.values())
            if not ready and began is None:
                began = t
            elif ready and began is not None:
                best, began = max(best, t - began), None
        if began is not None:
            best = max(best, end - began)
        return round(best, 1)

    def max_restarts(self, pod):
        return max((s[pod]["restarts"] for _t, s, _u in self.snaps if pod in s), default=None)


def wait(cond, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def current_pod():
    return sh(K + ["get", "pod", "-l", "app=traceguard", "-o", "jsonpath={.items[0].metadata.name}"]).stdout.strip()


def ready_pods():
    out = sh(K + ["get", "pods", "-l", "app=traceguard", "-o", "json"]).stdout
    return [p["metadata"]["name"] for p in json.loads(out)["items"]
            if not p["metadata"].get("deletionTimestamp")
            and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))]


def environment():
    def first_line(cmd):
        r = sh(cmd)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "unknown"
    server = "unknown"
    try:
        server = json.loads(sh(["kubectl", "--context", CTX, "version", "-o", "json"]).stdout)["serverVersion"]["gitVersion"]
    except Exception:
        pass
    ansible = shutil.which("ansible-playbook")
    return {"cluster": f"kind (single node), Kubernetes {server}", "kind": first_line(["kind", "version"]).split(" go")[0],
            "docker_engine": first_line(["docker", "version", "--format", "{{.Server.Version}}"]),
            "ansible": ("ansible-core " + (re.search(r"core ([\d.]+)", first_line([ansible, "--version"])) or [None, "unknown"])[1]) if ansible else "not used",
            "host": "WSL2 (Linux) on Windows 11 with Docker Desktop" if "microsoft" in platform.release().lower() else platform.platform(),
            "app_image": "traceguard:0.1.0 (replicas: 1, probes: startup/liveness/readiness)"}


def meta(sid, title):
    commit = sh(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
    # ignore the recorder's own output folder, or recording D would mark the tree dirty for E
    dirty = bool(sh(["git", "status", "--porcelain", "--", ".", ":(exclude)app/ui/recordings"]).stdout.strip())
    return {"id": sid, "title": title, "source": "recorded",
            "recorded_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "git_commit": commit + ("+uncommitted" if dirty else ""), "environment": environment()}


def timeline(obs, since=0.0):
    rows = [(t, "action", text) for t, text in obs.marks if t >= since] + [(t, c, e) for t, c, e in obs.events(since)]
    rows.sort(key=lambda r: r[0])
    base = rows[0][0] if rows else 0.0
    return [{"t_s": round(t - base, 1), "category": c, "event": e} for t, c, e in rows], base


def settle(obs, seconds):
    time.sleep(seconds)


def record_d():
    obs = Observer()
    obs.start()
    time.sleep(2)
    http("POST", "/simulate-failure", {"scenario": "none", "clear_incident_window": True})
    start_time_before = app_start_time()
    pod_before = current_pod()
    started = obs.mark("scenario D started: baseline, 1 pod Ready")

    # D1: container crash (same pod; kubelet restarts the container)
    t_crash = obs.mark("D1: POST /simulate-failure {scenario: crash}; the process exits about 1 s later")
    http("POST", "/simulate-failure", {"scenario": "crash"})
    ok1 = wait(lambda: any(s[1].get(pod_before, {}).get("restarts", 0) >= 1 and s[1][pod_before]["ready"] for s in obs.snaps[-3:]), 90)
    time.sleep(9)  # let Prometheus scrape after recovery so its view is complete

    # D2: delete the pod (ReplicaSet recreates it with a new name)
    pod_mid = current_pod()
    t_delete = obs.mark(f"D2: kubectl delete pod {pod_mid}")
    sh(K + ["delete", "pod", pod_mid, "--wait=false"])
    ok2 = wait(lambda: [p for p in ready_pods() if p != pod_mid], 120)
    pod_after = current_pod()
    fwd_ok = wait(lambda: (app_start_time() or 0) > (start_time_before or 0) + 1 and http("GET", "/ready").get("status") == "ready", 60)
    t_fwd = time.time() - obs.t0
    time.sleep(9)
    obs.stop()
    if not (ok1 and ok2):
        sys.exit("recovery did not complete within the timeout; nothing recorded")

    ev, _base = timeline(obs)
    t_exit = obs.first(r"container terminated", t_crash)
    t_ready1 = obs.first(rf"pod {re.escape(pod_before)} became Ready", (t_exit or t_crash))
    # D2 ready: first Ready event of a pod other than the one that was deleted
    t_ready2 = next((t for t, _c, text in obs.events() if t > t_delete and "became Ready" in text and pod_mid not in text), None)
    if None in (t_exit, t_ready1, t_ready2):
        sys.exit("could not find every expected state change in the observer log; nothing recorded")
    up0_d1 = [t for t, c, text in obs.events() if c == "prometheus" and text.endswith("-> 0") and t_crash <= t < t_delete]
    up0_d2 = [t for t, c, text in obs.events() if c == "prometheus" and text.endswith("-> 0") and t >= t_delete]
    end_t = obs.snaps[-1][0]
    gap1, gap2 = obs.no_ready_pod_seconds(t_crash, t_delete), obs.no_ready_pod_seconds(t_delete, end_t)
    # window = exactly this recording (plus a small margin), so earlier restarts are not counted
    window = int(time.time() - obs.t0) + 5
    changes = prom(f'changes(process_start_time_seconds{{job="traceguard"}}[{window}s])')
    restarts_after_crash = obs.max_restarts(pod_before)
    headline = (f"Container crash: Ready again {round(t_ready1 - t_exit, 1)} s after the process exited (same pod, restart count "
                f"0 to {restarts_after_crash}). Pod deleted: replacement pod Ready {round(t_ready2 - t_delete, 1)} s after the delete.")
    measurements = [
        {"name": "D1: process exit to pod Ready again", "value": round(t_ready1 - t_exit, 1), "unit": "s",
         "note": "kubelet restarted the container inside the same pod"},
        {"name": "D2: pod delete to replacement Ready", "value": round(t_ready2 - t_delete, 1), "unit": "s",
         "note": "ReplicaSet created a new pod with a new name"},
        {"name": "D2: delete to the local port-forward serving the new process", "value": round(t_fwd - t_delete, 1) if fwd_ok else None, "unit": "s",
         "note": "supervised forward reconnected without manual action" if fwd_ok else "did not reconnect in 60 s"},
        {"name": "D1: time with no Ready pod (observer, about 1 s resolution)", "value": gap1, "unit": "s",
         "note": "while no pod is Ready the app cannot emit traces or metrics"},
        {"name": "D2: time with no Ready pod (observer, about 1 s resolution)", "value": gap2, "unit": "s",
         "note": "the single replica is the only endpoint, so deleting it is a short outage"},
        {"name": "D1: Prometheus recorded up=0", "value": "yes" if up0_d1 else "no", "unit": "",
         "note": "only recorded if a 5 s scrape lands inside the outage"},
        {"name": "D2: Prometheus recorded up=0", "value": "yes" if up0_d2 else "no", "unit": "",
         "note": "only recorded if a 5 s scrape lands inside the outage"},
        {"name": "Process starts seen by Prometheus during this recording", "value": int(float(changes)) if changes else None, "unit": "",
         "note": f"changes in process_start_time over the last {window} s: one container restart plus one pod replacement"}]
    rec = {**meta("D", "Kubernetes pod failure and recovery"),
           "summary": {"headline": headline, "pod_before": pod_before, "pod_after_delete": pod_after,
                       "restart_count_after_crash": restarts_after_crash},
           "timeline": ev, "measurements": measurements,
           "notes": ["One run on one laptop; timings vary between runs and are not a benchmark.",
                     "Readiness gates traffic: a new pod receives requests only after /ready passes.",
                     "In-memory application state (fault flags, incident window and history) resets with the process.",
                     "Prometheus up=0 appeared in most but not all observed outages: a short outage can fall between two scrapes."]}
    return rec


def run_playbook(extra=()):
    cmd = ["ansible-playbook", "-i", "ansible/inventory.ini", "ansible/recover-deployment.yml", *extra]
    began = time.time()
    r = sh(cmd, env={**__import__("os").environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0"})
    return r, round(time.time() - began, 1)


def parse_playbook(out):
    result = re.search(r"RESULT: ([A-Z ()a-z]+)", out)
    recap = re.search(r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+skipped=(\d+)", out)
    after = re.search(r'after: (\{.*?\})"', out.replace('\\"', '"'))
    probe = {}
    if after:
        try:
            probe = {k: v for k, v in json.loads(after.group(1)).items() if k in ("ready", "http_status", "correct", "sku")}
        except json.JSONDecodeError:
            pass
    return {"result": result.group(1).strip() if result else None,
            "recap": dict(zip(("ok", "changed", "unreachable", "failed", "skipped"), map(int, recap.groups()))) if recap else None,
            "probe": probe}


def record_e():
    if not shutil.which("ansible-playbook"):
        sys.exit("ansible-playbook not on PATH (scenario E needs it)")
    obs = Observer()
    obs.start()
    time.sleep(2)
    obs.mark("scenario E started: baseline, 1 pod Ready")
    http("POST", "/simulate-failure", {"scenario": "wrong_result", "clear_incident_window": True})
    obs.mark("injected a silent failure: POST /simulate-failure {scenario: wrong_result}")
    wrong = 0
    for _ in range(6):
        wrong += 0 if http("POST", "/recommend", {"user_id": "record", "category": "electronics", "budget": 250})["correctness"]["correct"] else 1
    inc = http("GET", "/incidents")
    types = [i["type"] for i in inc["active"]]
    health, ready = http("GET", "/health")["status"], http("GET", "/ready")["status"]   # raises on non-200
    obs.mark(f"6 requests all returned HTTP 200 but {wrong}/6 results were wrong; /health says '{health}' and /ready says '{ready}'; "
             f"open incidents: {', '.join(types) or 'none'}")
    pod_before = current_pod()

    obs.mark("operator runs: ansible-playbook recover-deployment.yml --check (dry run)")
    dry, dry_s = run_playbook(["--check"])
    dry_info = parse_playbook(dry.stdout)
    obs.mark(f"dry run finished: {dry_info['result']} (changes made: {dry_info['recap']['changed'] if dry_info['recap'] else '?'})")

    t_run = obs.mark("operator runs: ansible-playbook recover-deployment.yml (rollout restart)")
    real, real_s = run_playbook()
    info = parse_playbook(real.stdout)
    t_done = obs.mark(f"playbook finished: {info['result']}; post-recovery probe {info['probe']}")
    sh(["scripts/port-forward.sh", "start", "traceguard"])
    time.sleep(6)
    after = http("POST", "/recommend", {"user_id": "record", "category": "electronics", "budget": 250})
    fault = http("GET", "/simulate-failure")["scenario"]
    obs.mark(f"after recovery: next request correct={after['correctness']['correct']}, injected fault state = {fault} (reset with the process)")
    time.sleep(3)
    obs.stop()
    if real.returncode != 0 or info["result"] != "SUCCESS":
        sys.exit(f"playbook did not succeed; nothing recorded:\n{real.stdout[-800:]}")

    ev, base = timeline(obs)
    gap = obs.no_ready_pod_seconds(t_run, t_done + 5)
    availability = ("with no moment without a Ready pod" if gap == 0 else f"with {gap} s without a Ready pod")
    headline = (f"Silent failure (HTTP 200, {wrong}/6 wrong results) detected as a correctness incident; the playbook's dry run changed "
                f"{dry_info['recap']['changed'] if dry_info['recap'] else '?'} things, the real run finished in {real_s} s with result "
                f"{info['result']} {availability}, and the next request was correct={after['correctness']['correct']}.")
    rec = {**meta("E", "Controlled Ansible recovery"),
           "summary": {"headline": headline, "pod_before": pod_before, "pod_after": current_pod(), "playbook_result": info["result"]},
           "timeline": ev,
           "measurements": [
               {"name": "Dry run (--check): changes made", "value": dry_info["recap"]["changed"] if dry_info["recap"] else None, "unit": "",
                "note": "read-only checks only; the restart is reported as 'would run'"},
               {"name": "Real run: tasks ok / changed / failed", "value": f"{info['recap']['ok']} / {info['recap']['changed']} / {info['recap']['failed']}" if info["recap"] else None, "unit": ""},
               {"name": "Real run: playbook wall time", "value": real_s, "unit": "s", "note": "includes waiting for the rolling update and the verification probe"},
               {"name": "Time with no Ready pod (observer, about 1 s resolution)", "value": gap, "unit": "s",
                "note": "rolling update with maxUnavailable: 0 keeps the old pod Ready until the new one is"},
               {"name": "Post-recovery probe (inside the pod)", "value": f"ready={info['probe'].get('ready')}, HTTP {info['probe'].get('http_status')}, correct={info['probe'].get('correct')}", "unit": ""}],
           "notes": ["One run on one laptop. The playbook is restricted to deployment traceguard in namespace traceguard of the kind-traceguard context; extra-vars cannot widen that (tested).",
                     "Rollback (recovery_action=rollback) exists and was verified locally; it is not part of this recording.",
                     "The injected fault lives in process memory, so any restart clears it. A restart does not fix a real bad release: that is what rollback is for."]}
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="all", choices=["D", "E", "all"])
    ap.add_argument("--out-dir", default=str(ROOT / "app" / "ui" / "recordings"))
    args = ap.parse_args()
    ctx = sh(["kubectl", "config", "current-context"]).stdout.strip()
    if ctx != CTX and CTX not in sh(["kubectl", "config", "get-contexts", "-o", "name"]).stdout.split():
        sys.exit(f"context {CTX} not found; deploy the kind cluster first (scripts/deploy.sh)")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for sid, fn in (("D", record_d), ("E", record_e)):
        if args.which in (sid, "all"):
            rec = fn()
            text = json.dumps(rec, indent=2) + "\n"
            if IPV4.search(text):
                sys.exit(f"scenario {sid}: recording contains an IP address; refusing to write it")
            (out / f"scenario_{sid.lower()}.json").write_text(text, encoding="utf-8")
            print(f"recorded scenario {sid}: {rec['summary']['headline']}")


if __name__ == "__main__":
    main()
