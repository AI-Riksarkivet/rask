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

# The frontend (@rask/api + remote functions) calls /api/*, so default the whole
# fleet to RASK_API_PREFIX=/api. The gateway/services' OWN default is /api/v1; a
# mismatch makes the gateway send /api/* to the core catch-all and you get 404s
# (e.g. the storage browser's /api/volumes/objects). Override to /api/v1 if needed.
export RASK_API_PREFIX="${RASK_API_PREFIX:-/api}"

# PORT_OFFSET shifts every port so a second fleet can run on one host without
# colliding (e.g. another user already holds the defaults): PORT_OFFSET=1000 →
# gateway :9888, core :9801, volumes :9803, … Then point the frontend at it:
#   PORT_OFFSET=1000 make dev-micro
#   VIEWER_BACKEND=http://localhost:9888 RASK_GATEWAY_URL=http://localhost:9888 make dev-frontends
OFFSET="${PORT_OFFSET:-0}"
GATEWAY_PORT="${GATEWAY_PORT:-$((8888 + OFFSET))}"
CORE_PORT="${CORE_PORT:-$((8801 + OFFSET))}"
SEARCH_PORT="${SEARCH_PORT:-$((8802 + OFFSET))}"
VOLUMES_PORT="${VOLUMES_PORT:-$((8803 + OFFSET))}"
RAY_PORT="${RAY_PORT:-$((8804 + OFFSET))}"
ORCH_PORT="${ORCH_PORT:-$((8810 + OFFSET))}"
CONTROLPLANE_PORT="${CONTROLPLANE_PORT:-$((8820 + OFFSET))}"

# Wire the gateway's upstreams to THIS fleet's per-service ports (else, when
# offset, it would route to whatever holds the default ports). No-op at OFFSET=0.
export RASK_CORE_API_URL="${RASK_CORE_API_URL:-http://127.0.0.1:${CORE_PORT}}"
export RASK_SEARCH_API_URL="${RASK_SEARCH_API_URL:-http://127.0.0.1:${SEARCH_PORT}}"
export RASK_VOLUMES_API_URL="${RASK_VOLUMES_API_URL:-http://127.0.0.1:${VOLUMES_PORT}}"
export RASK_RAY_API_URL="${RASK_RAY_API_URL:-http://127.0.0.1:${RAY_PORT}}"
export RASK_ORCH_API_URL="${RASK_ORCH_API_URL:-http://127.0.0.1:${ORCH_PORT}}"
export RASK_CONTROLPLANE_URL="${RASK_CONTROLPLANE_URL:-http://127.0.0.1:${CONTROLPLANE_PORT}}"

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
run controlplane "$CONTROLPLANE_PORT" controlplane:app env RASK_ORCHESTRATOR_AUTOSTART=false

echo "fleet up — gateway on http://127.0.0.1:${GATEWAY_PORT} (RASK_API_PREFIX=${RASK_API_PREFIX}; Ctrl-C to stop)"
if [ "$OFFSET" != "0" ]; then
  echo "  PORT_OFFSET=${OFFSET} → run the frontend with VIEWER_BACKEND=http://localhost:${GATEWAY_PORT} RASK_GATEWAY_URL=http://localhost:${GATEWAY_PORT}"
fi
wait
