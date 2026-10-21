#!/usr/bin/env bash
# Port-forward the in-cluster services to localhost.
#   scripts/port-forward.sh start | stop | restart
# A port-forward is bound to ONE pod: after a pod is replaced (delete, rollout) run `restart`.
set -euo pipefail
cd "$(dirname "$0")/.."
CTX="${K8S_CONTEXT:-kind-traceguard}"
NS=traceguard
PIDDIR=.pf
mkdir -p "$PIDDIR"

PORTS=("traceguard:8000:8000" "jaeger:16686:16686" "prometheus:9090:9090" "grafana:3000:3000")

stop() {
  for f in "$PIDDIR"/*.pid; do
    [ -e "$f" ] || continue
    kill "$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  done
}

start() {
  command -v kubectl >/dev/null || { echo "ERROR: kubectl not installed" >&2; exit 1; }
  for spec in "${PORTS[@]}"; do
    IFS=: read -r svc local remote <<<"$spec"
    kubectl --context "$CTX" -n "$NS" port-forward "svc/$svc" "$local:$remote" >"$PIDDIR/$svc.log" 2>&1 &
    echo $! >"$PIDDIR/$svc.pid"
  done
  sleep 3
  for spec in "${PORTS[@]}"; do
    IFS=: read -r svc local _ <<<"$spec"
    if kill -0 "$(cat "$PIDDIR/$svc.pid")" 2>/dev/null; then echo "forwarding $svc -> 127.0.0.1:$local"
    else echo "FAILED $svc (port $local busy?) see $PIDDIR/$svc.log" >&2; fi
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  *) echo "usage: $0 start|stop|restart" >&2; exit 2 ;;
esac
