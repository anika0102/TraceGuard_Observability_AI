#!/usr/bin/env python3
"""Drives scenarios A-C against a running TraceGuard API (stdlib only; works on Windows/Linux/macOS).

    python scripts/demo.py A|B|C|all [--base http://127.0.0.1:8000]

Scenarios D (pod failure) and E (Ansible recovery) need Kubernetes: see scripts/run-demo.sh.
Every number printed here comes from the live API; nothing is canned.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

REQ = {"user_id": "demo-user", "category": "electronics", "budget": 250}


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def fault(base, scenario, **kw):
    status, body = call(base, "POST", "/simulate-failure", {"scenario": scenario, "clear_incident_window": True, **kw})
    if status == 403:
        sys.exit("Fault injection is disabled (needs TRACEGUARD_ENV=local and TRACEGUARD_ENABLE_FAULT_INJECTION=true).")
    return body


def send(base, n):
    rows = []
    for _ in range(n):
        t = time.perf_counter()
        status, body = call(base, "POST", "/recommend", REQ)
        rows.append((status, (time.perf_counter() - t) * 1000, body))
    return rows


def show_requests(rows):
    for status, ms, b in rows:
        corr = b.get("correctness", {})
        print(f"  HTTP {status}  {ms:7.0f} ms  sku={((b.get('recommendation') or {}).get('sku'))}  "
              f"correct={corr.get('correct')}  trace_id={b.get('trace_id')}")


def show_incidents(base):
    _, inc = call(base, "GET", "/incidents")
    w = inc["window"]
    print(f"  window: n={w['samples']} error_rate={w['error_rate']:.0%} p50={w['p50_ms']}ms p95={w['p95_ms']}ms "
          f"incorrect={w['incorrect_rate']:.0%}")
    if not inc["active"]:
        print("  incidents: none open")
    for i in inc["active"]:
        print(f"  INCIDENT [{i['severity']}] {i['type']}: {i['summary']}")
        print(f"     evidence: {i['evidence']}")
        print(f"     trace_id: {i['trace_id']}  ->  {inc['jaeger_ui_url']}/trace/{i['trace_id']}" if i["trace_id"]
              else "     trace_id: none available")
        print(f"     {i['caveat']}")
        print(f"     remediation: {i['recommended_remediation']}")


def scenario_a(base):
    print("\n=== Scenario A: normal operation ===")
    fault(base, "none")
    print("  /health:", call(base, "GET", "/health")[1], " /ready:", call(base, "GET", "/ready")[1])
    show_requests(send(base, 6))
    show_incidents(base)
    print("  Next: open Jaeger (service 'traceguard') and Grafana to see these requests.")


def scenario_b(base):
    print("\n=== Scenario B: slow requests ===")
    fault(base, "slow", delay_ms=1500)
    show_requests(send(base, 6))
    show_incidents(base)
    fault(base, "none")
    print("  fault cleared. In Jaeger, compare recommend.lookup_catalog duration against a Scenario A trace.")


def scenario_c(base):
    print("\n=== Scenario C: silent correctness failure ===")
    fault(base, "wrong_result")
    rows = send(base, 6)
    show_requests(rows)
    print("  /health:", call(base, "GET", "/health")[1], " /ready:", call(base, "GET", "/ready")[1],
          " <- infrastructure view is green")
    all_200 = all(s == 200 for s, _, _ in rows)
    print(f"  every response HTTP 200: {all_200}; incorrect outcomes: "
          f"{sum(1 for _, _, b in rows if not b['correctness']['correct'])}/{len(rows)}")
    show_incidents(base)
    fault(base, "none")
    print("  fault cleared (application-level recovery: no restart needed for this injected fault).")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["A", "B", "C", "all"])
    p.add_argument("--base", default="http://127.0.0.1:8000")
    a = p.parse_args()
    try:
        for name, fn in (("A", scenario_a), ("B", scenario_b), ("C", scenario_c)):
            if a.scenario in (name, "all"):
                fn(a.base)
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {a.base}: {e.reason}. Start the app (README step 1) or port-forward first.")


if __name__ == "__main__":
    main()
