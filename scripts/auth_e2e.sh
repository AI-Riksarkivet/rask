#!/usr/bin/env bash
# End-to-end auth check: Dex (OIDC) + OpenFGA (authz, SQLite) against the live stack.
#
# Unlike a tuple-poking smoke test, this exercises the APP-SIDE WRITE PATH: the
# app is expected to SEED ownership tuples when resources are created. We never
# write a tuple by hand — every grant in this run comes from the app itself.
#
# Chain asserted (all with real Dex id_tokens, no manual FGA writes):
#   1. no token                  -> 401   (OIDC enforced)
#   2. alice creates a namespace -> 200   (app seeds owner:namespace for alice)
#        => OpenFGA now has  user:alice  owner  namespace:<ns>
#   3. alice creates a table     -> 200   (writer cascades from owner on parent;
#                                          app seeds owner:table + parent link)
#        => OpenFGA now has  user:alice  owner  table:<ns>$<tbl>
#                            namespace:<ns>  parent  table:<ns>$<tbl>
#   4. alice reads the table     -> 200   (reader cascades from owner)
#   5. alice writes the table    -> 2xx   (writer cascades from owner)
#   6. bob reads the table       -> 403   (bob holds NO grant)
#   7. bob writes the table      -> 403   (bob holds NO grant)
#
#   ./scripts/auth_e2e.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE=.docker/docker-compose.yml
# Postgres-backed OpenFGA by default. For the lighter SQLite stack:
#   AUTH_OVERLAY=.docker/docker-compose.auth.sqlite.yml ./scripts/auth_e2e.sh
AUTH="${AUTH_OVERLAY:-.docker/docker-compose.auth.yml}"
SERVER=http://localhost:2333
DEX=http://localhost:5556/dex
FGA=http://localhost:8080

NS="e2ens$$"                 # unique per run so reruns don't 409 on create
TBL="t1"
TID="${NS}\$${TBL}"         # table identifier on the wire ($ delimiter)
NS_OBJ="namespace:${NS}"
TBL_OBJ="table:${NS}\$${TBL}"

# Include the local host-port override (e.g. MinIO remapped off 9000/9001) when present,
# so the e2e doesn't clash with other containers already bound to those ports.
LOCAL=.docker/docker-compose.local.yml
compose() {
  if [ -f "$LOCAL" ]; then
    docker compose -f "$BASE" -f "$AUTH" -f "$LOCAL" "$@"
  else
    docker compose -f "$BASE" -f "$AUTH" "$@"
  fi
}

# Set KEEP_STACK=1 to leave the stack up on exit (e.g. so CI can dump logs, or
# for local debugging). Default: tear everything down, including volumes.
cleanup() {
  if [ "${KEEP_STACK:-0}" = "1" ]; then
    echo "== leaving stack up (KEEP_STACK=1) =="
    return
  fi
  echo "== tearing down =="
  compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== bring up SQLite auth stack (no Postgres) =="
compose up -d
until curl -fsS "$FGA/healthz" >/dev/null 2>&1; do sleep 1; done
until curl -fsS "$DEX/.well-known/openid-configuration" >/dev/null 2>&1; do sleep 1; done
# Recreate the server once its deps are ready so OpenFGA provisioning succeeds.
compose up -d --force-recreate --no-deps server
until curl -fsS "$SERVER/livez" >/dev/null 2>&1; do sleep 1; done
sleep 2

# The assertions live in `scripts/auth_chain.sh` — ONE copy, shared with `dagger call auth-chain`,
# which stands the same stack up as Dagger services instead of compose. Everything above this line is
# orchestration and is on its way out: when `.github/workflows/ci.yml` can be edited (a concurrent
# session holds it), that job moves to `dagger call auth-chain` and this file goes with it.
LANCE_E2E_AUTH_SERVER="$SERVER" LANCE_E2E_DEX="$DEX" LANCE_E2E_FGA="$FGA" \
  exec bash scripts/auth_chain.sh
