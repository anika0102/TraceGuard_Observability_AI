#!/usr/bin/env bash
# Integration verification. Each check reports PASS / FAIL / SKIP. SKIP = dependency not available here,
# with the exact command to run it yourself. Exit code is non-zero only if something FAILED.
#   scripts/verify.sh                      # API at http://127.0.0.1:8000
#   SKIP_ANSIBLE=1 scripts/verify.sh       # do not restart the deployment via Ansible
set -uo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE_URL:-http://127.0.0.1:8000}"
JAEGER="${JAEGER_URL:-http://127.0.0.1:16686}"
PROM="${PROM_URL:-http://127.0.0.1:9090}"
CTX="${K8S_CONTEXT:-kind-traceguard}"
NS=traceguard
PY="${PYTHON:-$(command -v python3 || command -v python)}"
FAILS=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1"; echo "      run yourself: $2"; }
jget() { "$PY" -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
post() { curl -fsS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
REC='{"user_id":"verify","category":"electronics","budget":250}'

# 1. API responds
api_up() { curl -fsS "$BASE/health" >/dev/null 2>&1 && curl -fsS "$BASE/ready" >/dev/null 2>&1; }
# A pod replacement (for example by the previous run's Ansible step) leaves the supervised port-forward
# reconnecting for a few seconds. `start` is idempotent and waits for it; it does nothing harmful otherwise.
if ! api_up && [ -x scripts/port-forward.sh ] && command -v kubectl >/dev/null 2>&1; then
  scripts/port-forward.sh start traceguard >/dev/null 2>&1
fi
if api_up; then
  pass "1. API responds (/health, /ready) at $BASE"
else
  fail "1. API not reachable at $BASE (start it: README step 1, or scripts/port-forward.sh start)"
  echo "Cannot continue without the API."; exit 1
fi

# 2. Correct vs incorrect business outcomes
post /simulate-failure '{"scenario":"none","clear_incident_window":true}' >/dev/null 2>&1 || true
GOOD=$(post /recommend "$REC")
if [ "$(echo "$GOOD" | jget "d['correctness']['correct']")" = "True" ]; then pass "2a. correct outcome validated as correct"
else fail "2a. normal request flagged incorrect: $GOOD"; fi
TRACE_ID=$(echo "$GOOD" | jget "d['trace_id']")
if post /simulate-failure '{"scenario":"wrong_result"}' >/dev/null 2>&1; then
  RESP=$(curl -s -w '\n%{http_code}' -X POST "$BASE/recommend" -H 'Content-Type: application/json' -d "$REC")
  BAD_CODE=$(echo "$RESP" | tail -n 1)
  BAD_BODY=$(echo "$RESP" | sed '$d')
  if [ "$BAD_CODE" = "200" ] && [ "$(echo "$BAD_BODY" | jget "d['correctness']['correct']")" = "False" ]; then
    pass "2b. silent failure: HTTP 200 but outcome flagged incorrect"
  else fail "2b. silent failure not detected (HTTP $BAD_CODE)"; fi
  post /simulate-failure '{"scenario":"none","clear_incident_window":true}' >/dev/null
else
  skip "2b. fault injection disabled on this deployment" "TRACEGUARD_ENV=local TRACEGUARD_ENABLE_FAULT_INJECTION=true"
fi

# 3. OpenTelemetry spans reach the trace backend (app -> Collector -> Jaeger)
if curl -fsS "$JAEGER/api/services" >/dev/null 2>&1; then
  FOUND=""
  for _ in $(seq 1 15); do
    N=$(curl -fsS "$JAEGER/api/traces/$TRACE_ID" 2>/dev/null | jget "len(d['data'][0]['spans']) if d.get('data') else 0" 2>/dev/null || echo 0)
    if [ "${N:-0}" -ge 5 ]; then FOUND=$N; break; fi
    sleep 1
  done
  if [ -n "$FOUND" ]; then pass "3. trace $TRACE_ID found in Jaeger with $FOUND spans"
  else fail "3. trace $TRACE_ID not in Jaeger after 15s (check collector logs; README Troubleshooting)"; fi
else
  skip "3. Jaeger not reachable at $JAEGER (compose/cluster not running?)" \
       "docker compose up -d --build  then  curl $JAEGER/api/traces/$TRACE_ID"
fi

# 4. Prometheus metrics exposed and scrapeable
if curl -fsS "$BASE/metrics" | grep -q '^traceguard_http_requests_total'; then pass "4a. /metrics exposes traceguard_http_requests_total"
else fail "4a. /metrics missing traceguard metrics"; fi
if curl -fsS "$PROM/-/ready" >/dev/null 2>&1; then
  UP=""
  for _ in $(seq 1 12); do
    UP=$(curl -fsSG "$PROM/api/v1/query" --data-urlencode 'query=up{job="traceguard"}' | jget "d['data']['result'][0]['value'][1]" 2>/dev/null || true)
    [ "$UP" = "1" ] && break
    sleep 2
  done
  if [ "$UP" = "1" ]; then pass "4b. Prometheus scrapes the app (up=1)"; else fail "4b. Prometheus target not up (got '${UP:-none}')"; fi
else
  skip "4b. Prometheus not reachable at $PROM" "curl '$PROM/api/v1/query?query=up{job=\"traceguard\"}'"
fi

# 5-7. Kubernetes + Ansible (optional layers)
if ! command -v kubectl >/dev/null 2>&1; then
  skip "5. kubectl not installed" "scripts/deploy.sh && kubectl --context $CTX -n $NS rollout status deploy/traceguard"
  skip "6. Ansible recovery needs kubectl + ansible" "pip install ansible-core && ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml"
  skip "7. post-recovery check needs the cluster" "scripts/run-demo.sh E"
elif ! kubectl --context "$CTX" -n "$NS" get deploy traceguard >/dev/null 2>&1; then
  skip "5. deployment traceguard not found in context $CTX" "scripts/deploy.sh"
  skip "6. Ansible recovery needs the deployment" "ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml"
  skip "7. post-recovery check needs the deployment" "scripts/run-demo.sh E"
else
  if kubectl --context "$CTX" -n "$NS" rollout status deploy/traceguard --timeout=120s >/dev/null 2>&1; then pass "5. Kubernetes deployment is ready"
  else fail "5. deployment not ready: kubectl --context $CTX -n $NS describe deploy/traceguard"; fi

  if [ "${SKIP_ANSIBLE:-0}" = "1" ]; then
    skip "6/7. skipped by SKIP_ANSIBLE=1" "ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml"
  elif ! command -v ansible-playbook >/dev/null 2>&1; then
    skip "6. ansible-playbook not installed" "pip install ansible-core && ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml"
    skip "7. depends on 6" "see 6"
  else
    if ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml --check >/dev/null 2>&1 \
       && ansible-playbook -i ansible/inventory.ini ansible/recover-deployment.yml; then
      pass "6. Ansible dry-run and restart workflow succeeded"
    else fail "6. Ansible playbook failed"; fi
    # The restart replaced the pod. A supervised forward reconnects by itself; wait for it so $BASE works again.
    [ -x scripts/port-forward.sh ] && scripts/port-forward.sh start traceguard >/dev/null 2>&1
    # stdout is the verdict; stderr is kept apart because `kubectl exec` can print harmless websocket warnings
    # ("Unknown stream id 1, discarding message") that must not turn a correct result into a failure.
    ERRF=$(mktemp)
    OUT=$(kubectl --context "$CTX" -n "$NS" exec deploy/traceguard -- python -c \
      "import urllib.request as u,json;r=u.Request('http://localhost:8000/recommend',json.dumps({'user_id':'v','category':'books','budget':100}).encode(),{'Content-Type':'application/json'});print(json.loads(u.urlopen(r).read())['correctness']['correct'])" 2>"$ERRF")
    if [ "$OUT" = "True" ]; then pass "7. service returns a correct result after recovery"; else fail "7. post-recovery request failed: stdout='$OUT' stderr='$(cat "$ERRF")'"; fi
    rm -f "$ERRF"
  fi
fi

echo; if [ "$FAILS" -eq 0 ]; then echo "verify: no failures (see SKIP lines for checks that could not run here)"; else echo "verify: $FAILS FAILED"; fi
exit "$FAILS"
