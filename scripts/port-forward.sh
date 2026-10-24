#!/usr/bin/env bash
# Port-forward the in-cluster services to localhost, detached from the launching shell.
#   scripts/port-forward.sh start | stop | restart | status
#
# - Forwards survive the shell that started them (setsid + nohup), including one-shot `wsl -- bash ...` calls.
# - PIDs are tracked in .pf/<service>.pid. `stop` only ever kills a PID whose command line is a
#   `kubectl --context <ctx> -n <ns> port-forward svc/<service>` started by this script; a stale or reused
#   PID is left alone. There is no pkill/killall, so unrelated port-forwards are never touched.
# - start/stop/status are idempotent. `status` exits 0 only if every forward is alive AND accepting connections.
# A port-forward is bound to ONE pod: after a pod is replaced (delete, rollout) run `restart`.
#
# Overrides (used by the tests): K8S_CONTEXT, K8S_NAMESPACE, PF_PIDDIR, PF_SERVICES="svc:local:remote ...".
set -uo pipefail
cd "$(dirname "$0")/.."

CTX="${K8S_CONTEXT:-kind-traceguard}"
NS="${K8S_NAMESPACE:-traceguard}"
PIDDIR="${PF_PIDDIR:-.pf}"
read -r -a SERVICES <<<"${PF_SERVICES:-traceguard:8000:8000 jaeger:16686:16686 prometheus:9090:9090 grafana:3000:3000}"
mkdir -p "$PIDDIR"

proc_cmd() {  # command line of a live PID (empty if the process does not exist)
  if [ -r "/proc/$1/cmdline" ]; then tr '\0' ' ' <"/proc/$1/cmdline"; else ps -o command= -p "$1" 2>/dev/null; fi
}

is_ours() {  # is_ours PID SERVICE -> 0 only for our own kubectl port-forward of that service
  local cmd
  cmd="$(proc_cmd "$1")"
  [[ "$cmd" == *kubectl* && "$cmd" == *"--context $CTX "* && "$cmd" == *"-n $NS "* \
     && "$cmd" == *" port-forward "* && "$cmd" == *"svc/$2 "* ]]
}

tcp_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

saved_pid() { cat "$PIDDIR/$1.pid" 2>/dev/null; }

start_one() {
  local svc=$1 local_port=$2 remote_port=$3 pid
  pid="$(saved_pid "$svc")"
  if [ -n "$pid" ] && is_ours "$pid" "$svc"; then
    echo "$svc: already running (pid $pid) -> 127.0.0.1:$local_port"; return 0
  fi
  rm -f "$PIDDIR/$svc.pid"                                  # stale or foreign pid file
  if tcp_open "$local_port"; then
    echo "$svc: FAILED, port $local_port is already in use by another process" >&2; return 1
  fi
  local launcher=(nohup)
  command -v setsid >/dev/null 2>&1 && launcher=(setsid nohup)
  # The wrapper records its own PID, then exec's kubectl, so the PID file holds the kubectl PID.
  "${launcher[@]}" sh -c 'echo $$ >"$1"; shift; exec "$@"' sh "$PIDDIR/$svc.pid" \
    kubectl --context "$CTX" -n "$NS" port-forward "svc/$svc" "$local_port:$remote_port" \
    >"$PIDDIR/$svc.log" 2>&1 </dev/null &
  for _ in $(seq 1 50); do                                   # up to ~10 s for the tunnel to accept connections
    pid="$(saved_pid "$svc")"
    if [ -n "$pid" ] && is_ours "$pid" "$svc" && tcp_open "$local_port"; then
      echo "$svc: forwarding 127.0.0.1:$local_port (pid $pid)"; return 0
    fi
    if [ -n "$pid" ] && ! is_ours "$pid" "$svc" && [ -z "$(proc_cmd "$pid")" ]; then break; fi   # died early
    sleep 0.2
  done
  echo "$svc: FAILED to start; see $PIDDIR/$svc.log" >&2
  tail -n 3 "$PIDDIR/$svc.log" >&2 2>/dev/null
  pid="$(saved_pid "$svc")"; [ -n "$pid" ] && is_ours "$pid" "$svc" && kill "$pid" 2>/dev/null
  rm -f "$PIDDIR/$svc.pid"
  return 1
}

stop_one() {
  local svc=$1 pid
  pid="$(saved_pid "$svc")"
  if [ -z "$pid" ]; then echo "$svc: not running"; return 0; fi
  if is_ours "$pid" "$svc"; then
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 25); do is_ours "$pid" "$svc" || break; sleep 0.2; done
    is_ours "$pid" "$svc" && kill -9 "$pid" 2>/dev/null
    echo "$svc: stopped (pid $pid)"
  elif [ -n "$(proc_cmd "$pid")" ]; then
    echo "$svc: pid $pid is not a TraceGuard port-forward (PID reused?); leaving it alone"
  else
    echo "$svc: not running (removed stale pid file)"
  fi
  rm -f "$PIDDIR/$svc.pid"
}

status_one() {
  local svc=$1 local_port=$2 pid
  pid="$(saved_pid "$svc")"
  if [ -n "$pid" ] && is_ours "$pid" "$svc"; then
    if tcp_open "$local_port"; then echo "$svc: running (pid $pid), 127.0.0.1:$local_port accepting connections"; return 0; fi
    echo "$svc: running (pid $pid) but 127.0.0.1:$local_port NOT accepting connections (pod replaced? run restart)"; return 1
  fi
  echo "$svc: stopped"; return 1
}

each() {  # each FUNCTION: run it for every service; non-zero exit if any call failed
  local fn=$1 rc=0 spec svc l r
  for spec in "${SERVICES[@]}"; do
    IFS=: read -r svc l r <<<"$spec"
    "$fn" "$svc" "$l" "$r" || rc=1
  done
  return $rc
}

case "${1:-start}" in
  start)
    command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not installed" >&2; exit 1; }
    each start_one ;;
  stop) each stop_one ;;
  restart) each stop_one; each start_one ;;
  status) each status_one ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
