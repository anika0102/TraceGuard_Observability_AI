"""In-pod probe run by the recovery playbook via `kubectl exec ... python -c`.

Checks /ready and sends one real /recommend request, then prints a single JSON line. It lives in a file
(loaded by a literal path in the playbook) so that `-e` extra-vars cannot substitute code that gets executed
inside the application pod.
"""
import json
import urllib.request


def call(path, body=None):
    req = urllib.request.Request(
        "http://localhost:8000" + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


rs, ready = call("/ready")
ps, rec = call("/recommend", {"user_id": "ansible-probe", "category": "electronics", "budget": 250})
print(json.dumps({
    "ready": ready["status"],
    "http_status": ps,
    "correct": rec["correctness"]["correct"],
    "sku": (rec["recommendation"] or {}).get("sku"),
    "trace_id": rec["trace_id"],
}))
