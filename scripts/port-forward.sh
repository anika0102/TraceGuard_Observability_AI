#!/usr/bin/env bash
# Port-forward the in-cluster services to localhost: detached, supervised and self-reconnecting.
#   scripts/port-forward.sh start | stop | restart | status [service]
#   (with no service: traceguard, jaeger, prometheus and grafana; with one name: only that service)
#
# - Each forward runs under a small supervisor (setsid + nohup), so it survives the launching shell AND is
#   re-created automatically when the pod behind it is replaced (rollout, delete, crash). A plain
#   `kubectl port-forward` exits with "lost connection to pod" in that case.
# - The supervisor's PID is tracked in .pf/<service>.pid. `stop` only ever signals a PID whose command line is
#   a `kubectl --context <ctx> -n <ns> port-forward svc/<service>` started by this script, and the supervisor
#   kills its own kubectl child on exit. A stale or reused PID is left alone; there is no pkill/killall.
# - The PID file is a lease: a supervisor whose PID file was removed or replaced exits at its next respawn, and
#   a supervisor gives up after PF_MAX_QUICK_FAILURES consecutive instant failures (cluster gone, bad name).
# - start/stop/restart/status are idempotent. `start` also waits for a reconnecting forward to come back.
#   `status` exits 0 only if every selected forward is alive AND accepting connections.
#
# Overrides (also used by the tests): K8S_CONTEXT, K8S_NAMESPACE, PF_PIDDIR, PF_SERVICES="svc:local:remote ...",
# PF_PROC_DIR, PF_MAX_QUICK_FAILURES (default 30), PF_RESPAWN_DELAY (seconds, default 1).
set -uo pipefail

CTX="${K8S_CONTEXT:-kind-traceguard}"
NS="${K8S_NAMESPACE:-traceguard}"
PIDDIR="${PF_PIDDIR:-.pf}"
PROC_DIR="${PF_PROC_DIR:-/proc}"
read -r -a SERVICES <<<"${PF_SERVICES:-traceguard:8000:8000 jaeger:16686:16686 prometheus:9090:9090 grafana:3000:3000}"
TARGET=""

proc_cmd() {  # command line of a PID; empty if the process is gone. Real read errors are reported, not hidden.
  local f="$PROC_DIR/$1/cmdline" out
  if [ -d "$PROC_DIR" ]; then
    # One read attempt, no check-then-read race. "2>&1" comes first so the shell's own redirection error
    # (for example ENOENT because the process just exited) is captured instead of leaking to the terminal.
    if out=$(tr '\0' ' ' 2>&1 <"$f"); then printf '%s' "$out"; return 0; fi
    [ -e "$f" ] || return 0   # the process exited between lookup and read: the expected race, nothing to report
    echo "port-forward.sh: unexpected error reading $f: $out" >&2
    return 0
  fi
  ps -o command= -p "$1" 2>/dev/null   # no /proc (macOS)
}

is_ours() {  # is_ours PID SERVICE -> 0 only for our own kubectl port-forward (or its supervisor) of that service
  local cmd
  cmd="$(proc_cmd "$1")"
  [[ "$cmd" == *kubectl* && "$cmd" == *"--context $CTX "* && "$cmd" == *"-n $NS "* \
     && "$cmd" == *" port-forward "* && "$cmd" == *"svc/$2 "* ]]
}

tcp_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

saved_pid() { cat "$PIDDIR/$1.pid" 2>/dev/null; }

wait_accepting() {  # wait_accepting PID SERVICE PORT SECONDS -> 0 once ours+alive+accepting
  local pid=$1 svc=$2 port=$3 tries=$(( $4 * 5 ))
  for _ in $(seq 1 "$tries"); do
    is_ours "$pid" "$svc" && tcp_open "$port" && return 0
    sleep 0.2
  done
  return 1
}

IFS= read -r -d '' SUPERVISOR <<'EOF' || true
pidfile=$1; shift
echo $$ >"$pidfile"
child=""
trap 'if [ -n "$child" ]; then kill "$child" 2>/dev/null; fi; exit 0' TERM INT
quick=0
while :; do
  started=$(date +%s)
  "$@" & child=$!
  wait "$child"
  child=""
  # The PID file is the lease. If it no longer names this supervisor we were stopped or replaced: exit.
  [ "$(cat "$pidfile" 2>/dev/null)" = "$$" ] || exit 0
  if [ $(( $(date +%s) - started )) -lt 5 ]; then quick=$((quick + 1)); else quick=0; fi
  if [ "$quick" -ge "${PF_MAX_QUICK_FAILURES:-30}" ]; then
    echo "port-forward supervisor: giving up after $quick consecutive instant failures" >&2; exit 1
  fi
  sleep "${PF_RESPAWN_DELAY:-1}" & child=$!
  wait "$child"
  child=""
done
EOF

start_one() {
  local svc=$1 local_port=$2 remote_port=$3 pid
  pid="$(saved_pid "$svc")"
  if [ -n "$pid" ] && is_ours "$pid" "$svc"; then
    if tcp_open "$local_port"; then
      echo "$svc: already running (pid $pid) -> 127.0.0.1:$local_port"; return 0
    fi
    if wait_accepting "$pid" "$svc" "$local_port" 15; then   # our forward is reconnecting after a pod change
      echo "$svc: reconnected (pid $pid) -> 127.0.0.1:$local_port"; return 0
    fi
    echo "$svc: running (pid $pid) but 127.0.0.1:$local_port is not accepting connections after 15 s;" \
         "try: $0 restart $svc" >&2
    return 1
  fi
  rm -f "$PIDDIR/$svc.pid"                                  # stale or foreign pid file
  if tcp_open "$local_port"; then
    echo "$svc: FAILED, port $local_port is already in use by another process" >&2; return 1
  fi
  local launcher=(nohup)
  command -v setsid >/dev/null 2>&1 && launcher=(setsid nohup)
  "${launcher[@]}" sh -c "$SUPERVISOR" sh "$PIDDIR/$svc.pid" \
    kubectl --context "$CTX" -n "$NS" port-forward "svc/$svc" "$local_port:$remote_port" \
    >"$PIDDIR/$svc.log" 2>&1 </dev/null &
  for _ in $(seq 1 50); do                                   # up to ~10 s for the tunnel to accept connections
    pid="$(saved_pid "$svc")"
    if [ -n "$pid" ] && is_ours "$pid" "$svc" && tcp_open "$local_port"; then
      echo "$svc: forwarding 127.0.0.1:$local_port (pid $pid)"; return 0
    fi
    if [ -n "$pid" ] && [ -z "$(proc_cmd "$pid")" ]; then break; fi   # supervisor already gone (gave up)
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
    kill "$pid" 2>/dev/null                                  # supervisor's TERM trap kills its own child
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
    echo "$svc: running (pid $pid) but 127.0.0.1:$local_port NOT accepting connections (reconnecting; if this persists run: $0 restart $svc)"; return 1
  fi
  echo "$svc: stopped"; return 1
}

each() {  # each FUNCTION: run it for every selected service; non-zero exit if any call failed
  local fn=$1 rc=0 spec svc l r
  for spec in "${SERVICES[@]}"; do
    IFS=: read -r svc l r <<<"$spec"
    [ -n "$TARGET" ] && [ "$svc" != "$TARGET" ] && continue
    "$fn" "$svc" "$l" "$r" || rc=1
  done
  return $rc
}

main() {
  cd "$(dirname "$0")/.." || exit 1
  mkdir -p "$PIDDIR"
  TARGET="${2:-}"   # optional: act on a single service instead of all of them
  if [ -n "$TARGET" ]; then  # reject unknown names before doing anything
    local known="" match=0 spec
    for spec in "${SERVICES[@]}"; do known="$known ${spec%%:*}"; [ "${spec%%:*}" = "$TARGET" ] && match=1; done
    if [ "$match" -eq 0 ]; then echo "unknown service '$TARGET' (known:$known)" >&2; exit 2; fi
  fi
  case "${1:-start}" in
    start)
      command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not installed" >&2; exit 1; }
      each start_one ;;
    stop) each stop_one ;;
    restart)
      command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not installed" >&2; exit 1; }
      each stop_one; each start_one ;;
    status) each status_one ;;
    *) echo "usage: $0 start|stop|restart|status [service]" >&2; exit 2 ;;
  esac
}

# Run main only when executed, so tests can `source` this file and call proc_cmd/is_ours directly.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
