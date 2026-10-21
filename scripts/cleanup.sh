#!/usr/bin/env bash
# Remove ONLY what this project created: its namespace, its kind cluster, and its compose stack.
#   scripts/cleanup.sh
set -uo pipefail
cd "$(dirname "$0")/.."
CTX="${K8S_CONTEXT:-kind-traceguard}"

[ -x scripts/port-forward.sh ] && scripts/port-forward.sh stop 2>/dev/null
rm -rf .pf

if command -v docker >/dev/null 2>&1; then
  docker compose -f docker-compose.yml down -v --remove-orphans 2>/dev/null && echo "compose stack removed"
fi

if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx traceguard; then
  kind delete cluster --name traceguard   # deletes only the 'traceguard' kind cluster
elif command -v kubectl >/dev/null 2>&1 && [ "$CTX" = "minikube" ]; then
  kubectl --context "$CTX" delete namespace traceguard --ignore-not-found   # never touches other namespaces
fi
echo "cleanup done. (Optional) docker rmi traceguard:0.1.0"
