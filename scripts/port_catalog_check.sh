#!/usr/bin/env bash
# port_catalog_check.sh: run the Rust table client's tests against the REAL catalog (ADR 0222).
#
# Starts the Python catalog app on real pylance over a local directory with authentication off
# (scripts/port_catalog_fixture.py), runs `cargo test -p lakehouse-tables --test catalog` against it, and stops it.
# Without the fixture those tests skip, so this is what runs them.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
port="${RASK_PORT_CATALOG_PORT:-8765}"
log="$(mktemp)"

cd "$root"
uv run --no-sync scripts/port_catalog_fixture.py --port "$port" >"$log" 2>&1 &
fixture=$!
stop_fixture() {
  kill "$fixture" 2>/dev/null || true
}
trap stop_fixture EXIT

for _ in $(seq 1 60); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$port/livez" && break
  sleep 1
done
curl -s -m 2 -o /dev/null "http://127.0.0.1:$port/livez" || { echo "!! the catalog fixture did not come up; its log:" >&2; tail -20 "$log" >&2; exit 1; }

cd "$root/rust"
RASK_PORT_CATALOG_URL="http://127.0.0.1:$port" cargo test --locked -p lakehouse-tables --test catalog -- --nocapture
