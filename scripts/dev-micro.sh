#!/usr/bin/env bash
# Local microservice fleet — no extra dependencies (replaces honcho).
#
# Brings up the per-domain backends as background processes, prefixes
# each one's logs, and shuts the whole group down on Ctrl-C. Bring up the deps
# first: `make ray-up`; S3/HCP comes from .env. Usually invoked via
# `make dev-micro` (which runs `uv sync --all-packages` first). Ports are
# overridable via *_PORT env vars. (The orchestrator process died at P7a; the
# core-api/search-api/volumes-api trio died in the R6/R20 media wave — the S3
# object browser now rides the media-plane VIEWER, which this fleet starts so
# the lakehouse storage browser has a live /api/explorer backend in dev. See
# ADR 0112.)
set -euo pipefail

# NOTE: do NOT bash-source .env here. Every service loads it itself via
# python-dotenv (load_dotenv), which correctly parses JSON-list settings like
# RASK_CORS_ORIGINS=["..."]; bash sourcing strips the quotes and breaks parsing.
# We only export vars that are NOT in .env and that the services require.

export RAY_ENABLE_UV_RUN_RUNTIME_ENV=0   # documented Ray/uv gotcha

# The frontend (@rask/api + remote functions) calls /api/*, so default the whole
# fleet to RASK_API_PREFIX=/api. The services' OWN default is /api/v1, and the
# chart's routes publish /api/flows, /api/ray, … unchanged — a mismatch is a 404
# at the service (e.g. the compute zone's /api/ray/jobs).
export RASK_API_PREFIX="${RASK_API_PREFIX:-/api}"

# PORT_OFFSET shifts every port so a second fleet can run on one host without
# colliding (e.g. another user already holds the defaults): PORT_OFFSET=1000 →
# compute :9804, viewer :9101, …
OFFSET="${PORT_OFFSET:-0}"
COMPUTE_PORT="${COMPUTE_PORT:-$((8804 + OFFSET))}"
FLOWS_PORT="${FLOWS_PORT:-$((8840 + OFFSET))}"
NOTIFICATIONS_PORT="${NOTIFICATIONS_PORT:-$((8850 + OFFSET))}"
VIEWER_PORT="${VIEWER_PORT:-$((8101 + OFFSET))}"
SEARCH_PORT="${SEARCH_PORT:-$((8102 + OFFSET))}"
ANNOTATOR_PORT="${ANNOTATOR_PORT:-$((8103 + OFFSET))}"
# The viewer reads its own VIEWER_PORT (media-plane settings), exported here so
# its `run()` row binds the port this script names under PORT_OFFSET.
export VIEWER_PORT

# Kill the whole process group on exit so no uvicorn lingers.
trap 'trap - INT TERM EXIT; echo; echo "stopping fleet..."; kill 0' INT TERM EXIT

run() {  # run <name> <port> <module> [extra env assignments...]
  local name=$1 port=$2 module=$3
  shift 3
  ( "$@" uv run --no-sync uvicorn "$module" --host 127.0.0.1 --port "$port" 2>&1 | sed "s/^/[$name] /" ) &
}

run compute      "$COMPUTE_PORT"      compute:app
# The studio flow-builder's backend: /api/flows/{catalog,validate,runs}. Needs nothing to boot — the
# catalog is declared in-process and validation is pure — so it comes up green with no Ray, no Serve
# and no sidecar; a `model` node then fails honestly, naming the address it could not reach. The
# durable Dapr Workflow lane stays OFF here on purpose (it starts only when DAPR_GRPC_PORT is set,
# which nothing in this fleet sets), so a local run executes inline.
run flows        "$FLOWS_PORT"        flows:app
# The notification plane: /api/notifications/inbox{,/unread,/seen,/dismiss}. It BOOTS with no sidecar
# — `DaprActor` only mounts the callback routes and `ActorRuntime.register_actor` is process-LOCAL
# (it builds type info and stores an ActorManager in a dict; daprd learns the entity list afterwards
# by polling /dapr/config). So the door comes up and `/api/health`, `/livez`, `/readyz` all answer.
# What it does NOT do here is serve an inbox: `require_actor_plane` sees `actors_registered = True`
# (registration succeeded — it never needed the sidecar), the route calls the ActorProxy, and the
# proxy dials 127.0.0.1:3500 where nothing is listening. Nothing translates that transport failure,
# so an inbox route answers 500, not a 503 naming the cause. Read a 500 from :8850 here as "no
# sidecar", not as a broken service. The bus subscription is DECLARED too (GET /dapr/subscribe serves
# it), but nothing delivers to it without a sidecar, and the reconciler that would close that gap is
# S2 — so in this fleet the badge is empty either way.
run notifications "$NOTIFICATIONS_PORT" notifications:app
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

echo "fleet up — compute :${COMPUTE_PORT} flows :${FLOWS_PORT} notifications :${NOTIFICATIONS_PORT} viewer :${VIEWER_PORT} search :${SEARCH_PORT} annotator :${ANNOTATOR_PORT} (RASK_API_PREFIX=${RASK_API_PREFIX}; Ctrl-C to stop)"
echo "  no edge runs here: the zones' /api needs one on RASK_EDGE_URL — the in-cluster Gateway via"
echo "  \`make dev-frontends-k3s\`, or \`make dev-zone ZONE=<z>\` for a zone against its mocks."
wait
