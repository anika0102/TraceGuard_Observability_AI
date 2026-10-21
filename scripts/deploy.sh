#!/usr/bin/env bash
# Create a local cluster (kind by default, or minikube) and deploy the app plus the observability stack.
#   scripts/deploy.sh                   # kind
#   CLUSTER_TOOL=minikube scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER_TOOL="${CLUSTER_TOOL:-kind}"
IMAGE="${IMAGE:-traceguard:0.1.0}"
NS=traceguard

missing=()
for t in docker kubectl "$CLUSTER_TOOL"; do command -v "$t" >/dev/null 2>&1 || missing+=("$t"); done
if [ ${#missing[@]} -gt 0 ]; then
  echo "ERROR: missing prerequisites: ${missing[*]}" >&2
  echo "  kind:     https://kind.sigs.k8s.io/docs/user/quick-start/#installation" >&2
  echo "  kubectl:  https://kubernetes.io/docs/tasks/tools/" >&2
  echo "  docker:   https://docs.docker.com/get-docker/" >&2
  exit 1
fi

if [ "$CLUSTER_TOOL" = "kind" ]; then
  CTX="kind-traceguard"
  if ! kind get clusters 2>/dev/null | grep -qx traceguard; then
    kind create cluster --config k8s/kind-cluster.yaml
  fi
else
  CTX="minikube"
  minikube status >/dev/null 2>&1 || minikube start
fi
KC=(kubectl --context "$CTX")

scripts/build.sh
if [ "$CLUSTER_TOOL" = "kind" ]; then
  kind load docker-image "$IMAGE" --name traceguard
else
  minikube image load "$IMAGE"
fi

"${KC[@]}" apply -f k8s/namespace.yaml

# Single source of truth: observability configs live in observability/ and are mounted via ConfigMaps.
apply_cm() { "${KC[@]}" -n "$NS" create configmap "$@" --dry-run=client -o yaml | "${KC[@]}" apply -f -; }
apply_cm otel-collector-config --from-file=config.yaml=observability/otel-collector.yaml
apply_cm prometheus-config --from-file=prometheus.yaml=observability/prometheus.yaml \
  --from-file=alerts.yml=observability/alerts.yml
apply_cm grafana-datasources --from-file=observability/grafana/provisioning/datasources/datasources.yml
apply_cm grafana-dashboard-provider --from-file=observability/grafana/provisioning/dashboards/dashboards.yml
apply_cm grafana-dashboards --from-file=observability/grafana/dashboards/traceguard.json

"${KC[@]}" apply -f k8s/observability/jaeger.yaml -f k8s/observability/otel-collector.yaml \
  -f k8s/observability/prometheus.yaml -f k8s/observability/grafana.yaml
"${KC[@]}" apply -f k8s/app-configmap.yaml -f k8s/app-deployment.yaml -f k8s/app-service.yaml

for d in jaeger otel-collector prometheus grafana traceguard; do
  "${KC[@]}" -n "$NS" rollout status "deployment/$d" --timeout=240s
done

cat <<EOF

Deployed to context $CTX, namespace $NS.
Next:  scripts/port-forward.sh start
  app http://127.0.0.1:8000 (console: /console)   Jaeger http://127.0.0.1:16686
  Prometheus http://127.0.0.1:9090                Grafana http://127.0.0.1:3000
EOF
