#!/usr/bin/env bash
# Storage-agnostic e2e against RustFS: run the SAME catalog lifecycle test (tests/e2e/test_e2e.py)
# that the MinIO stack runs, but with the bytes on RustFS — proving the catalog is S3-agnostic.
# (Auth is OFF here; this exercises the data path. For the governance/auth stack see auth_e2e.sh.)
#
#   ./scripts/rustfs_e2e.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE=.docker/docker-compose.yml
RUSTFS=.docker/docker-compose.rustfs.yml
SERVER=http://localhost:2333

compose() { docker compose -f "$BASE" -f "$RUSTFS" "$@"; }

cleanup() {
  if [ "${KEEP_STACK:-0}" = "1" ]; then echo "== leaving stack up (KEEP_STACK=1) =="; return; fi
  echo "== tearing down =="
  compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== bring up catalog on RustFS (MinIO services gated off by the overlay) =="
compose up -d --build
# Recreate the server once the RustFS bucket bootstrap has completed.
compose up -d --force-recreate --no-deps server
until curl -fsS "$SERVER/livez" >/dev/null 2>&1; do sleep 1; done
sleep 2

echo "== run the storage-agnostic lifecycle e2e against RustFS =="
LANCE_REST_E2E_URL="$SERVER" uv run --no-sync pytest tests/e2e/test_e2e.py -v
echo "== DONE — same catalog lifecycle passed on RustFS =="
