#!/usr/bin/env bash
# Governance end-to-end: bring up the FULL stack (catalog + auth + lineage, with catalog ->
# lineage emission ON) and verify the dataops/governance loop — authz + provenance authorship
# + medallion lineage — with real Dex id_tokens and no hand-written tuples.
#
# Stack = base + auth (OIDC/OpenFGA) + lineage (AGE) + governance (lineage-api + emit on).
#
#   ./scripts/governance_e2e.sh            # pytest e2e (tests/e2e/test_governance_e2e.py)
#   DEMO=1 ./scripts/governance_e2e.sh     # run the narrated demo instead
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE=.docker/docker-compose.yml
AUTH="${AUTH_OVERLAY:-.docker/docker-compose.auth.yml}"
LINEAGE=.docker/docker-compose.lineage.yml
GOV=.docker/docker-compose.governance.yml
LOCAL=.docker/docker-compose.local.yml

SERVER=http://localhost:2333
LINEAGE_URL=http://localhost:8000
DEX=http://localhost:5556/dex
FGA=http://localhost:8080

compose() {
  local files=(-f "$BASE" -f "$AUTH" -f "$LINEAGE" -f "$GOV")
  [ -f "$LOCAL" ] && files+=(-f "$LOCAL")
  docker compose "${files[@]}" "$@"
}

cleanup() {
  if [ "${KEEP_STACK:-0}" = "1" ]; then echo "== leaving stack up (KEEP_STACK=1) =="; return; fi
  echo "== tearing down =="
  compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== bring up governance stack (catalog + auth + lineage + emit) =="
compose up -d --build
until curl -fsS "$FGA/healthz" >/dev/null 2>&1; do sleep 1; done
until curl -fsS "$DEX/.well-known/openid-configuration" >/dev/null 2>&1; do sleep 1; done
# Recreate the server once deps are ready so OpenFGA provisioning succeeds.
compose up -d --force-recreate --no-deps server
until curl -fsS "$SERVER/livez" >/dev/null 2>&1; do sleep 1; done
until curl -fsS "$LINEAGE_URL/livez" >/dev/null 2>&1; do sleep 1; done
sleep 2

export LANCE_E2E_AUTH_SERVER="$SERVER" LANCE_E2E_LINEAGE_URL="$LINEAGE_URL" LANCE_E2E_DEX="$DEX"
export CATALOG_URL="$SERVER" LINEAGE_URL="$LINEAGE_URL" DEX_URL="$DEX"

if [ "${DEMO:-0}" = "1" ]; then
  echo "== run narrated governance demo =="
  uv run --no-sync scripts/governance_demo.py
else
  echo "== run governance e2e =="
  uv run --no-sync pytest tests/e2e/test_governance_e2e.py -v
fi
echo "== DONE =="
