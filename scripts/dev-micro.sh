#!/usr/bin/env bash
# Local microservice fleet — no extra dependencies (replaces honcho).
#
# Brings up the gateway + per-domain backends as background processes, prefixes
# each one's logs, and shuts the whole group down on Ctrl-C. Bring up the deps
# first: `make ray-up`; S3/HCP comes from .env. Usually invoked via
# `make dev-micro` (which runs `uv sync --all-packages` first). Ports are
# overridable via *_PORT env vars. (The orchestrator process died at P7a; the
# core-api/search-api/volumes-api trio died in the R6/R20 media wave — the S3
# object browser now rides the media-plane VIEWER, which this fleet starts so
# the lakehouse storage browser has a live /api/explorer backend in dev. See
# docs/architecture/lance-ns-merge.md P7.)
set -euo pipefail

# NOTE: do NOT bash-source .env here. Every service loads it itself via
# python-dotenv (load_dotenv), which correctly parses JSON-list settings like
# RASK_CORS_ORIGINS=["..."]; bash sourcing strips the quotes and breaks parsing.
# We only export vars that are NOT in .env and that the services require.

export RAY_ENABLE_UV_RUN_RUNTIME_ENV=0   # documented Ray/uv gotcha

# The frontend (@rask/api + remote functions) calls /api/*, so default the whole
# fleet to RASK_API_PREFIX=/api. The gateway/services' OWN default is /api/v1; a
# mismatch makes the gateway route /api/* rows wrong and you get 404s (e.g. the
# compute zone's /api/ray/jobs). Override to /api/v1 if needed.
export RASK_API_PREFIX="${RASK_API_PREFIX:-/api}"

# PORT_OFFSET shifts every port so a second fleet can run on one host without
# colliding (e.g. another user already holds the defaults): PORT_OFFSET=1000 →
# gateway :9888, compute :9804, viewer :9101, … Then point the frontend at it:
#   PORT_OFFSET=1000 make dev-micro
#   VIEWER_BACKEND=http://localhost:9888 RASK_GATEWAY_URL=http://localhost:9888 make dev-frontends
OFFSET="${PORT_OFFSET:-0}"
GATEWAY_PORT="${GATEWAY_PORT:-$((8888 + OFFSET))}"
COMPUTE_PORT="${COMPUTE_PORT:-$((8804 + OFFSET))}"
CONTROLPLANE_PORT="${CONTROLPLANE_PORT:-$((8820 + OFFSET))}"
FLOWS_PORT="${FLOWS_PORT:-$((8840 + OFFSET))}"
VIEWER_PORT="${VIEWER_PORT:-$((8101 + OFFSET))}"
SEARCH_PORT="${SEARCH_PORT:-$((8102 + OFFSET))}"
ANNOTATOR_PORT="${ANNOTATOR_PORT:-$((8103 + OFFSET))}"

# Wire the gateway's upstreams to THIS fleet's per-service ports (else, when
# offset, it would route to whatever holds the default ports). No-op at OFFSET=0.
export RASK_COMPUTE_URL="${RASK_COMPUTE_URL:-http://127.0.0.1:${COMPUTE_PORT}}"
export RASK_CONTROLPLANE_URL="${RASK_CONTROLPLANE_URL:-http://127.0.0.1:${CONTROLPLANE_PORT}}"
export RASK_FLOWS_URL="${RASK_FLOWS_URL:-http://127.0.0.1:${FLOWS_PORT}}"
export RASK_EXPLORER_VIEWER_URL="${RASK_EXPLORER_VIEWER_URL:-http://127.0.0.1:${VIEWER_PORT}}"
export RASK_EXPLORER_SEARCH_URL="${RASK_EXPLORER_SEARCH_URL:-http://127.0.0.1:${SEARCH_PORT}}"
export RASK_EXPLORER_ANNOTATOR_URL="${RASK_EXPLORER_ANNOTATOR_URL:-http://127.0.0.1:${ANNOTATOR_PORT}}"
# The viewer reads its own VIEWER_PORT (media-plane settings), exported here so
# its `run()` row and the gateway row above always agree under PORT_OFFSET.
export VIEWER_PORT

# Kill the whole process group on exit so no uvicorn lingers.
trap 'trap - INT TERM EXIT; echo; echo "stopping fleet..."; kill 0' INT TERM EXIT

run() {  # run <name> <port> <module> [extra env assignments...]
  local name=$1 port=$2 module=$3
  shift 3
  ( "$@" uv run --no-sync uvicorn "$module" --host 127.0.0.1 --port "$port" 2>&1 | sed "s/^/[$name] /" ) &
}

run gateway      "$GATEWAY_PORT"      gateway:app
run compute      "$COMPUTE_PORT"      compute:app
run controlplane "$CONTROLPLANE_PORT" controlplane:app
# The studio flow-builder's backend: /api/flows/{catalog,validate,runs}. Needs nothing to boot — the
# catalog is declared in-process and validation is pure — so it comes up green with no Ray, no Serve
# and no sidecar; a `model` node then fails honestly, naming the address it could not reach. The
# durable Dapr Workflow lane stays OFF here on purpose (it starts only when DAPR_GRPC_PORT is set,
# which nothing in this fleet sets), so a local run executes inline.
run flows        "$FLOWS_PORT"        flows:app
# The media-plane viewer: /api/explorer/* (incl. the lakehouse storage browser's
# /api/explorer/object* routes). Its DatasetRegistry is lazy, so it boots without a
# staged corpus — dataset routes then 404 honestly while the objects browser works.
run viewer       "$VIEWER_PORT"       viewer.main:app
# The other two thirds of the media plane. Until 2026-07-28 the fleet started ONLY the viewer, so the
# annotator zone's BFF (which proxies /annotator/api/annotations/* to :8103) 502'd against a fleet that
# looked "up" — the canvas could load a page image but never read or save a single annotation. Both
# are lazy over the same MEDIA_* registry as the viewer, so they boot without a staged corpus and
# 404 honestly until one exists (`uv run python scripts/seed_demo_corpus.py` makes one locally).
run search       "$SEARCH_PORT"       search.main:app
run annotator    "$ANNOTATOR_PORT"    annotator.main:app

echo "fleet up — gateway on http://127.0.0.1:${GATEWAY_PORT} (RASK_API_PREFIX=${RASK_API_PREFIX}; Ctrl-C to stop)"
if [ "$OFFSET" != "0" ]; then
  echo "  PORT_OFFSET=${OFFSET} → run the frontend with VIEWER_BACKEND=http://localhost:${GATEWAY_PORT} RASK_GATEWAY_URL=http://localhost:${GATEWAY_PORT}"
fi
wait
