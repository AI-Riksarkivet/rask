#!/usr/bin/env bash
# Live medallion demo on RustFS — watch real data + real lineage happen.
#
# Brings up RustFS (S3-compatible object store) + the lineage service, then runs the driver
# (scripts/medallion_demo.py), which writes/evolves REAL Lance datasets on RustFS and emits a REAL
# OpenLineage event after each step. Open the thin UI to watch the DAG build and silver evolve v1→v2:
#
#       http://localhost:8000/ui/
#
#   ./scripts/medallion_demo.sh             # bring up, run the driver, leave the stack up
#   KEEP_STACK=0 ./scripts/medallion_demo.sh   # tear the stack down afterwards
#
# The catalog (catalog.main) is NOT needed here — this demo is the compute/data + provenance loop.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Host ports are overridable so the demo coexists with a local MinIO/service already on 9000/8000:
#   DEMO_S3_PORT=9100 DEMO_LINEAGE_PORT=8001 ./scripts/medallion_demo.sh
export DEMO_S3_PORT="${DEMO_S3_PORT:-9000}"
export DEMO_LINEAGE_PORT="${DEMO_LINEAGE_PORT:-8000}"
export DEMO_WEB_PORT="${DEMO_WEB_PORT:-5173}"

# Record the actual endpoints so a bare `python scripts/medallion_demo.py --step N` finds the stack
# (no env-var prefix needed). The driver auto-loads this file; real env vars still override it.
cat > .medallion-demo.env <<EOF
S3_ENDPOINT=http://localhost:${DEMO_S3_PORT}
LINEAGE_URL=http://localhost:${DEMO_LINEAGE_PORT}
S3_ACCESS_KEY=rustfsadmin
S3_SECRET_KEY=rustfsadmin
S3_BUCKET=lakehouse
EOF

compose() {
  docker compose \
    -f .docker/docker-compose.yml \
    -f .docker/docker-compose.rustfs.yml \
    -f .docker/docker-compose.lineage.yml \
    -f .docker/docker-compose.governance.yml \
    -f .docker/docker-compose.demo.yml "$@"
}

cleanup() {
  if [ "${KEEP_STACK:-1}" = "1" ]; then echo "== leaving stack up (KEEP_STACK=1) — UI at http://localhost:8000/ui/ =="; return; fi
  echo "== tearing down =="
  compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== bring up RustFS + the lineage service + the SvelteKit UI =="
compose up -d --build rustfs-perms rustfs lineage-postgres lineage-api web

echo "== wait for the lineage service (http://localhost:${DEMO_LINEAGE_PORT}/livez) =="
until curl -fsS "http://localhost:${DEMO_LINEAGE_PORT}/livez" >/dev/null 2>&1; do sleep 2; done

UI="http://localhost:${DEMO_WEB_PORT}/"
echo "== open the live view now, then watch it build: ${UI} =="
echo "   (zero-dep fallback UI also at http://localhost:${DEMO_LINEAGE_PORT}/ui/)"
echo "== run the driver: real Lance on RustFS + real OpenLineage (one step every few seconds) =="
S3_ENDPOINT="http://localhost:${DEMO_S3_PORT}" S3_ACCESS_KEY=rustfsadmin S3_SECRET_KEY=rustfsadmin \
  S3_BUCKET=lakehouse LINEAGE_URL="http://localhost:${DEMO_LINEAGE_PORT}" STEP_DELAY="${STEP_DELAY:-2.5}" \
  uv run --no-sync scripts/medallion_demo.py "$@"

echo
echo "== DONE — the medallion DAG is live at ${UI} =="
echo "   trigger steps yourself (you are the producer):"
echo "     S3_ENDPOINT=http://localhost:${DEMO_S3_PORT} LINEAGE_URL=http://localhost:${DEMO_LINEAGE_PORT} \\"
echo "       uv run scripts/medallion_demo.py --step 1   # then --step 2, 3, 4, 5"
