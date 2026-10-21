#!/usr/bin/env bash
# Build the application image. Usage: scripts/build.sh   (IMAGE=traceguard:0.1.0 by default)
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-traceguard:0.1.0}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed or not on PATH. Install Docker Desktop (https://docs.docker.com/get-docker/)." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: the Docker daemon is not reachable. Start Docker Desktop and retry." >&2
  exit 1
fi

docker build -f docker/Dockerfile -t "$IMAGE" .
echo "Built $IMAGE (k8s/app-deployment.yaml references traceguard:0.1.0; keep the tags in sync)."
