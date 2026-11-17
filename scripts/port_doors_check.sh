#!/usr/bin/env bash
# port_doors_check.sh: put one request corpus to lineage's doors in Python and in Rust, side by side on the same real
# collaborators, and print every difference (ADR 0236).
#
# Starts the real Python catalog (scripts/port_catalog_fixture.py), the pinned RustFS with its STS and the stand-in
# ServiceAccount issuer it trusts (scripts/port_outbox_check.sh with KEEP_UP=1), the pinned OpenFGA on the in-memory
# datastore with a preshared key (scripts/openfga_server.sh), and an identity provider the requests' tokens are signed
# by (scripts/port_doors_fixture.py issuer). `setup` makes the governed tables and the grants; then the Python lineage
# app (uvicorn, lineage.main:app) and the Rust doors (the lineage crate's doors_serve example) start on one environment,
# each logging in to the store as lineage's own ServiceAccount, and `corpus` stages outbox events, parks deliveries and
# puts every request to both. Exits non-zero when any difference is not one ADR 0236 keeps.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
base="${RASK_PORT_DOORS_BASE_PORT:-9340}"
catalog_port=$base fga_port=$((base + 1)) grpc_port=$((base + 2)) issuer_port=$((base + 3))
python_port=$((base + 4)) rust_port=$((base + 5)) store_port=$((base + 6)) store_issuer_port=$((base + 7))
work="$(mktemp -d)"
key="doors-check-$RANDOM$RANDOM"
pids=()

stop() {
  kill "${pids[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$work" "${store_work:-}"
}
trap stop EXIT
up() {
  for _ in $(seq 1 120); do curl -s -m 2 -o /dev/null "$1" && return 0; sleep 1; done
  echo "!! $2 did not come up; its log:" >&2
  tail -30 "$work/$2.log" >&2
  exit 1
}

cd "$root"
(cd rust && cargo build --locked --quiet -p lineage --example doors_serve)
uv run --no-sync scripts/port_catalog_fixture.py --port "$catalog_port" >"$work/catalog.log" 2>&1 &
pids+=($!)
"$(bash scripts/openfga_server.sh)" run --datastore-engine memory --playground-enabled=false --metrics-enabled=false --log-level error \
  --http-addr "127.0.0.1:$fga_port" --grpc-addr "127.0.0.1:$grpc_port" --authn-method preshared --authn-preshared-keys "$key" \
  >"$work/openfga.log" 2>&1 &
pids+=($!)
uv run --no-sync scripts/port_doors_fixture.py issuer "$issuer_port" >"$work/issuer.log" 2>&1 &
pids+=($!)
stored="$(KEEP_UP=1 RASK_PORT_STORE_PORT="$store_port" RASK_PORT_ISSUER_PORT="$store_issuer_port" bash scripts/port_outbox_check.sh | tail -1)"
pids+=($(sed -E 's/.*\(pids ([0-9]+) ([0-9]+),.*/\1 \2/' <<<"$stored"))
store_work="$(sed -E 's/.*, (.*)\)$/\1/' <<<"$stored")"
store_endpoint="http://127.0.0.1:$store_port" store_issuer="http://127.0.0.1:$store_issuer_port" bucket=rask-control
up "http://127.0.0.1:$catalog_port/livez" catalog
up "http://127.0.0.1:$fga_port/healthz" openfga
up "http://127.0.0.1:$issuer_port/keys" issuer

ids="$(uv run --no-sync scripts/port_doors_fixture.py setup "$work" "http://127.0.0.1:$catalog_port" "http://127.0.0.1:$fga_port" "$key")"
echo "$key" >"$work/fga-token"
curl -sf "$store_issuer/token?sub=system:serviceaccount:rask:rask-sa-lineage" >"$work/store-token"
export RASK_OIDC_ENABLED=true RASK_OIDC_ISSUER="http://127.0.0.1:$issuer_port" RASK_OIDC_AUDIENCE=rask-lineage RASK_OIDC_IDP_ID=idp
export RASK_OIDC_ALLOW_INSECURE=true RASK_FGA_ENABLED=true RASK_FGA_API_URL="http://127.0.0.1:$fga_port" RASK_FGA_TOKEN_FILE="$work/fga-token"
export ${ids?} RASK_FGA_ROOT_OBJECT=estate:rask LANCE_AUDIT_ENABLED=false
export LINEAGE_CATALOG_URL="http://127.0.0.1:$catalog_port" LINEAGE_PROVENANCE_NAMESPACE='lance_catalog$port$doors' LINEAGE_MAX_BODY_BYTES=65536
export LINEAGE_OUTBOX_URI="s3://$bucket/_lineage_outbox" LINEAGE_S3_ENDPOINT="$store_endpoint" LINEAGE_S3_STS_ENDPOINT="$store_endpoint"
export LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE="$work/store-token" LINEAGE_S3_SESSION_SECONDS=900

uv run --no-sync uvicorn lineage.main:app --host 127.0.0.1 --port "$python_port" --log-level warning >"$work/python.log" 2>&1 &
pids+=($!)
rust/target/debug/examples/doors_serve "$rust_port" >"$work/rust.log" 2>&1 &
pids+=($!)
up "http://127.0.0.1:$python_port/livez" python
up "http://127.0.0.1:$rust_port/livez" rust

if [ "${KEEP_UP:-}" = 1 ]; then
  trap - EXIT
  echo "corpus: uv run --no-sync scripts/port_doors_fixture.py corpus $work http://127.0.0.1:$python_port http://127.0.0.1:$rust_port" \
    "http://127.0.0.1:$issuer_port $store_endpoint $bucket $store_issuer (pids ${pids[*]}; env in $work/env)"
  export -p | grep -E ' (RASK|LINEAGE|LANCE)_' >"$work/env"
  exit 0
fi
uv run --no-sync scripts/port_doors_fixture.py corpus "$work" "http://127.0.0.1:$python_port" "http://127.0.0.1:$rust_port" \
  "http://127.0.0.1:$issuer_port" "$store_endpoint" "$bucket" "$store_issuer"
