#!/usr/bin/env bash
# Local microservice fleet — no extra dependencies (replaces honcho).
#
# Brings up the gateway + per-domain backends as background processes, prefixes
# each one's logs, and shuts the whole group down on Ctrl-C. Bring up the deps
# first: `make ray-up`, `make pg-up` (+ `make pg-migrate`); S3/HCP comes from
# .env. Usually invoked via `make dev-micro` (which runs `uv sync --all-packages`
# first). Ports are overridable via *_PORT env vars.
set -euo pipefail

# NOTE: do NOT bash-source .env here. Every service loads it itself via
# python-dotenv (load_dotenv), which correctly parses JSON-list settings like
# RASK_CORS_ORIGINS=["..."]; bash sourcing strips the quotes and breaks parsing.
# We only export vars that are NOT in .env and that the services require.

export RAY_ENABLE_UV_RUN_RUNTIME_ENV=0   # documented Ray/uv gotcha
export RASK_VIEWER_INPUT="${RASK_VIEWER_INPUT:-s3://images-batch}"
export RASK_VIEWER_OUTPUT="${RASK_VIEWER_OUTPUT:-s3://images-batch-alto}"

GATEWAY_PORT="${GATEWAY_PORT:-8888}"
CORE_PORT="${CORE_PORT:-8801}"
SEARCH_PORT="${SEARCH_PORT:-8802}"
VOLUMES_PORT="${VOLUMES_PORT:-8803}"
RAY_PORT="${RAY_PORT:-8804}"
ORCH_PORT="${ORCH_PORT:-8810}"

# Only the orchestrator process runs the loop. We force it OFF for every other
# service (regardless of what .env says) so there is exactly one orchestrator.
# Set ORCH_AUTOSTART=false to bring the fleet up without submitting any jobs.
ORCH_AUTOSTART="${ORCH_AUTOSTART:-true}"

# Kill the whole process group on exit so no uvicorn lingers.
trap 'trap - INT TERM EXIT; echo; echo "stopping fleet..."; kill 0' INT TERM EXIT

run() {  # run <name> <port> <module> [extra env assignments...]
  local name=$1 port=$2 module=$3
  shift 3
  ( "$@" uv run --no-sync uvicorn "$module" --host 127.0.0.1 --port "$port" 2>&1 | sed "s/^/[$name] /" ) &
}

run gateway     "$GATEWAY_PORT" gateway:app
run core-api    "$CORE_PORT"    core_api:app    env RASK_ORCHESTRATOR_AUTOSTART=false
run search-api  "$SEARCH_PORT"  search_api:app  env RASK_ORCHESTRATOR_AUTOSTART=false
run volumes-api "$VOLUMES_PORT" volumes_api:app env RASK_ORCHESTRATOR_AUTOSTART=false
run ray-api     "$RAY_PORT"     ray_api:app     env RASK_ORCHESTRATOR_AUTOSTART=false
run orchestrator "$ORCH_PORT"   orchestrator:app env RASK_ORCHESTRATOR_AUTOSTART="$ORCH_AUTOSTART"

echo "fleet up — gateway on http://127.0.0.1:${GATEWAY_PORT} (Ctrl-C to stop)"
wait
