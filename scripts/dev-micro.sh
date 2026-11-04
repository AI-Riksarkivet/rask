#!/usr/bin/env bash
# The console's backend on the host: the notifications inbox as a background process, its logs prefixed, stopped on
# Ctrl-C. Usually invoked via `make dev-micro` (which runs `uv sync --all-packages` first). The port is overridable via
# NOTIFICATIONS_PORT, and PORT_OFFSET shifts it so a second copy can run on one host.
set -euo pipefail

# NOTE: do NOT bash-source .env here. The service loads it itself via python-dotenv (load_dotenv), which correctly parses
# JSON-list settings like RASK_CORS_ORIGINS=["..."]; bash sourcing strips the quotes and breaks parsing.

# The console calls /api/*, so default to RASK_API_PREFIX=/api; the service's OWN default is /api/v1, and a mismatch is
# a 404 at the service.
export RASK_API_PREFIX="${RASK_API_PREFIX:-/api}"

OFFSET="${PORT_OFFSET:-0}"
NOTIFICATIONS_PORT="${NOTIFICATIONS_PORT:-$((8850 + OFFSET))}"

# Kill the whole process group on exit so no uvicorn lingers.
trap 'trap - INT TERM EXIT; echo; echo "stopping..."; kill 0' INT TERM EXIT

# The inbox: /api/notifications/inbox{,/unread,/seen,/dismiss}. It BOOTS with no sidecar — `DaprActor` only mounts the
# callback routes and `ActorRuntime.register_actor` is process-LOCAL (it builds type info and stores an ActorManager in a
# dict; daprd learns the entity list afterwards by polling /dapr/config). So the door comes up and `/api/health`,
# `/livez`, `/readyz` all answer. What it does NOT do here is serve an inbox: the route calls the ActorProxy, and the
# proxy dials 127.0.0.1:3500 where nothing is listening, so an inbox route answers 500, not a 503 naming the cause. Read
# a 500 from :8850 here as "no sidecar", not as a broken service. The bus subscription is DECLARED too (GET
# /dapr/subscribe serves it), but nothing delivers to it without a sidecar.
( uv run --no-sync uvicorn notifications:app --host 127.0.0.1 --port "$NOTIFICATIONS_PORT" 2>&1 | sed "s/^/[notifications] /" ) &

echo "inbox up — notifications :${NOTIFICATIONS_PORT} (RASK_API_PREFIX=${RASK_API_PREFIX}; Ctrl-C to stop)"
echo "  no edge runs here: the console's /api needs one on RASK_EDGE_URL — the in-cluster Gateway via \`make dev-k3s\`."
wait
