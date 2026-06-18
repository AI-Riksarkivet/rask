#!/usr/bin/env bash
# Build one per-service image, run it, assert /api/v1/health returns 200.
# Usage: .docker/smoke-build.sh <project> <port> [healthpath]
set -euo pipefail
proj="$1"; port="$2"; healthpath="${3:-/api/v1/health}"
img="${proj}:dev"
echo ">> building ${img}"
docker buildx build -f ".docker/${proj}.dockerfile" -t "${img}" --load .
cname="smoke-${proj}"
docker rm -f "${cname}" >/dev/null 2>&1 || true
echo ">> running ${img}"
# Minimal env so Settings() constructs; sqlite avoids needing postgres.
docker run -d --name "${cname}" -p "${port}:${port}" \
  -e RASK_VIEWER_INPUT=s3://images-batch \
  -e RASK_VIEWER_OUTPUT=s3://images-batch-alto \
  -e RASK_ORCHESTRATOR_AUTOSTART=false \
  "${img}" >/dev/null
trap 'docker rm -f "${cname}" >/dev/null 2>&1 || true' EXIT
echo ">> waiting for health"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${port}${healthpath}" >/dev/null 2>&1; then
    echo "OK ${proj} healthy"; exit 0
  fi
  sleep 2
done
echo "FAIL ${proj} never became healthy"; docker logs "${cname}" | tail -30; exit 1
