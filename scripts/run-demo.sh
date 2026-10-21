#!/usr/bin/env bash
# Run a demo scenario.   scripts/run-demo.sh [A|B|C|D|E|all]     (default: all)
#   A-C need only a running API (BASE_URL, default http://127.0.0.1:8000).
#   D-E need the Kubernetes deployment (scripts/deploy.sh) and the port-forwards.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
CTX="${K8S_CONTEXT:-kind-traceguard}"
NS=traceguard
PY="${PYTHON:-$(command -v python3 || command -v python)}"
KC=(kubectl --context "$CTX" -n "$NS")
WHICH="${1:-all}"

need_k8s() {
  command -v kubectl >/dev/null || { echo "SKIP: kubectl not installed (see README 'Prerequisites')"; return 1; }
  "${KC[@]}" get deploy traceguard >/dev/null 2>&1 || { echo "SKIP: deployment not found in context $CTX. Run scripts/deploy.sh"; return 1; }
}
post() { curl -fsS -X POST "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2"; }
REC='{"user_id":"demo-user","category":"electronics","budget":250}'

scenario_d() {
  echo; echo "=== Scenario D: pod failure and recovery ==="
  need_k8s || return 0
  echo "-- D1: container crash (same pod, RESTARTS increments: container restart by kubelet)"
  "${KC[@]}" get pods -l app=traceguard
  post /simulate-failure '{"scenario":"crash"}'; echo
  sleep 4
  "${KC[@]}" wait --for=condition=ready pod -l app=traceguard --timeout=90s
  "${KC[@]}" get pods -l app=traceguard

  echo "-- D2: delete the pod (new pod name: pod recreation by the ReplicaSet)"
  OLD=$("${KC[@]}" get pod -l app=traceguard -o jsonpath='{.items[0].metadata.name}')
  "${KC[@]}" delete pod "$OLD" --wait=false
  sleep 2
  "${KC[@]}" get pods -l app=traceguard
  "${KC[@]}" wait --for=condition=ready pod -l app=traceguard --timeout=120s
  NEW=$("${KC[@]}" get pod -l app=traceguard -o jsonpath='{.items[0].metadata.name}')
  echo "old pod: $OLD   new pod: $NEW"
  scripts/port-forward.sh restart >/dev/null
  echo "-- request after recovery:"; post /recommend "$REC" | "$PY" -c "import sys,json;d=json.load(sys.stdin);print('correct =',d['correctness']['correct'],' trace_id =',d['trace_id'])"
  echo "Telemetry during the outage: scrapes fail (Prometheus 'up' = 0, 'Process restarts' panel increments);"
  echo "no traces are produced while no pod serves traffic; Jaeger keeps traces from before the failure."
}

scenario_e() {
  echo; echo "=== Scenario E: Ansible-assisted recovery ==="
  need_k8s || return 0
  command -v ansible-playbook >/dev/null || { echo "SKIP: ansible-playbook not installed. Run: pip install ansible-core  (Linux/macOS/WSL2)"; return 0; }
  echo "-- inject a silent failure"
  post /simulate-failure '{"scenario":"wrong_result","clear_incident_window":true}'; echo
  for _ in 1 2 3 4 5 6; do post /recommend "$REC" >/dev/null; done
  curl -fsS "$BASE_URL/incidents" | "$PY" -c "import sys,json;d=json.load(sys.stdin);print('open incidents:',[i['type'] for i in d['active']])"
  echo "-- dry run"
  ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml --check
  echo "-- real run"
  ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml
  scripts/port-forward.sh restart >/dev/null
  echo "-- after recovery (fault state was in-memory, so the restart cleared it):"
  post /recommend "$REC" | "$PY" -c "import sys,json;d=json.load(sys.stdin);print('correct =',d['correctness']['correct'])"
}

case "$WHICH" in
  A|B|C) "$PY" scripts/demo.py "$WHICH" --base "$BASE_URL" ;;
  D) scenario_d ;;
  E) scenario_e ;;
  all) "$PY" scripts/demo.py all --base "$BASE_URL"; scenario_d; scenario_e ;;
  *) echo "usage: $0 [A|B|C|D|E|all]" >&2; exit 2 ;;
esac
