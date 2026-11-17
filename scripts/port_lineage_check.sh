#!/usr/bin/env bash
# port_lineage_check.sh: prove lineage's Rust BINARY on real collaborators, beside the real Python lineage service
# (ADR 0238).
#
# Builds `lineage` (the debug binary, whose CloudEvent type `rask.check.handling-panics` stands in for a native
# library's panic), then starts the real Python catalog (scripts/port_catalog_fixture.py), the pinned OpenFGA on the
# in-memory datastore with a preshared key (scripts/openfga_server.sh), the identity provider the requests' tokens are
# signed by (scripts/port_doors_fixture.py issuer), the pinned RustFS with its STS, the stand-in ServiceAccount issuer it
# trusts and the chart's lineage policy (scripts/port_outbox_check.sh with KEEP_UP=1), and the pinned nats-server with
# JetStream and a token (scripts/nats_server.sh). scripts/port_lineage_fixture.py then adds the catalog proxy, the
# stand-in secret store, the OTLP sink and the graph backend, starts each service from one environment shaped like the
# chart's, and runs checks (a) to (f). CHECK_ONLY=a,c runs a subset; KEEP_UP=1 leaves the collaborators running and prints
# how to run the checks against them. Exits non-zero when any check fails.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
# Seven ports: from RASK_PORT_LINEAGE_BASE_PORT up where it is named, else seven free ones.
if [ -n "${RASK_PORT_LINEAGE_BASE_PORT:-}" ]; then
  ports=($(seq "$RASK_PORT_LINEAGE_BASE_PORT" $((RASK_PORT_LINEAGE_BASE_PORT + 6))))
else
  ports=($("$root/scripts/free_port.sh" 7))
fi
catalog_port=${ports[0]} fga_port=${ports[1]} grpc_port=${ports[2]} issuer_port=${ports[3]} nats_port=${ports[4]}
store_port=${ports[5]} store_issuer_port=${ports[6]}
work="$(mktemp -d)"
key="lineage-check-$RANDOM$RANDOM"
nats_token="lineage-check-bus-$RANDOM$RANDOM"
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
(cd rust && CARGO_INCREMENTAL=0 cargo build --locked --quiet -j 2 -p lineage --bin lineage)
uv run --no-sync scripts/port_catalog_fixture.py --port "$catalog_port" >"$work/catalog.log" 2>&1 &
pids+=($!)
"$(bash scripts/openfga_server.sh)" run --datastore-engine memory --playground-enabled=false --metrics-enabled=false --log-level error \
  --http-addr "127.0.0.1:$fga_port" --grpc-addr "127.0.0.1:$grpc_port" --authn-method preshared --authn-preshared-keys "$key" \
  >"$work/openfga.log" 2>&1 &
pids+=($!)
uv run --no-sync scripts/port_doors_fixture.py issuer "$issuer_port" >"$work/issuer.log" 2>&1 &
pids+=($!)
mkdir -p "$work/nats"
"$(bash scripts/nats_server.sh)" -js -a 127.0.0.1 -p "$nats_port" --auth "$nats_token" -sd "$work/nats" >"$work/nats.log" 2>&1 &
pids+=($!)
stored="$(KEEP_UP=1 RASK_PORT_STORE_PORT="$store_port" RASK_PORT_ISSUER_PORT="$store_issuer_port" bash scripts/port_outbox_check.sh | tail -1)"
pids+=($(sed -E 's/.*\(pids ([0-9]+) ([0-9]+),.*/\1 \2/' <<<"$stored"))
store_work="$(sed -E 's/.*, (.*)\)$/\1/' <<<"$stored")"
up "http://127.0.0.1:$catalog_port/livez" catalog
up "http://127.0.0.1:$fga_port/healthz" openfga
up "http://127.0.0.1:$issuer_port/keys" issuer
for _ in $(seq 1 30); do (echo >"/dev/tcp/127.0.0.1/$nats_port") 2>/dev/null && break; sleep 1; done

export CHECK_RUST_BIN="$root/rust/target/debug/lineage"
export CHECK_CATALOG_URL="http://127.0.0.1:$catalog_port" CHECK_FGA_URL="http://127.0.0.1:$fga_port" CHECK_FGA_KEY="$key"
export CHECK_ISSUER="http://127.0.0.1:$issuer_port" CHECK_NATS_URL="nats://127.0.0.1:$nats_port" CHECK_NATS_TOKEN="$nats_token"
export CHECK_STORE_ENDPOINT="http://127.0.0.1:$store_port" CHECK_STORE_ISSUER="http://127.0.0.1:$store_issuer_port"
export CHECK_BUCKET=rask-control CHECK_STORE_WORK="$store_work"
if [ "${KEEP_UP:-}" = 1 ]; then
  trap - EXIT
  export -p | grep -E ' CHECK_' >"$work/env"
  echo "check: source $work/env && uv run --no-sync python scripts/port_lineage_fixture.py check $work (pids ${pids[*]})"
  exit 0
fi
uv run --no-sync python scripts/port_lineage_fixture.py check "$work"
