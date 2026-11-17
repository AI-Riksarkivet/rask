#!/usr/bin/env bash
# port_lanes_check.sh: run lineage's Rust lanes against REAL collaborators (ADR 0235).
#
# Starts the real Python catalog on real pylance (scripts/port_catalog_fixture.py), the stand-in ServiceAccount issuer
# (scripts/port_outbox_fixture.py issuer) and the pinned RustFS (scripts/rustfs_server.sh) trusting it, with a root pair
# made for this run and read from files, and provisions the bucket and two identities' canned policies with the
# provisioning hook's own tool (scripts/rustfs_admin.py): the producer's, which stages into the outbox and drops what the
# bus took (put and delete there, as the chart grants maintenance), and the relay's,
# as the chart renders it for lineage (`lance.objectStorePolicy.lineage` with the relay on). Then runs
# `cargo test -p lineage --test lanes_live`, whose tests each start the pinned nats-server (scripts/nats_server.sh) and
# call the real Python producer and lane (scripts/port_lanes_fixture.py), and stops every server.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
catalog_port="${RASK_PORT_CATALOG_PORT:-8766}"
store_port="${RASK_PORT_STORE_PORT:-9321}"
issuer_port="${RASK_PORT_ISSUER_PORT:-9322}"
bucket=rask-control
work="$(mktemp -d)"
endpoint="http://127.0.0.1:$store_port"
issuer="http://127.0.0.1:$issuer_port"
store="$(bash "$root/scripts/rustfs_server.sh")"
bash "$root/scripts/nats_server.sh" >/dev/null

cd "$root"
stop() {
  kill "${catalog_pid:-}" "${issuer_pid:-}" "${store_pid:-}" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$work"
}
trap stop EXIT

mkdir -p "$work/data" "$work/root" "$work/issuer"
echo rask-check-root >"$work/root/access_key"
head -c 24 /dev/urandom | base64 | tr -d '/+=' >"$work/root/secret_key"

uv run --no-sync scripts/port_catalog_fixture.py --port "$catalog_port" >"$work/catalog.log" 2>&1 &
catalog_pid=$!
uv run --no-sync scripts/port_outbox_fixture.py issuer "$issuer_port" "$work/issuer" >"$work/issuer.log" 2>&1 &
issuer_pid=$!
for _ in $(seq 1 30); do curl -sf -o /dev/null "$issuer/.well-known/openid-configuration" && break; sleep 1; done

env -i PATH="$PATH" HOME="$work" \
  RUSTFS_VOLUMES="$work/data" RUSTFS_ADDRESS="127.0.0.1:$store_port" \
  RUSTFS_ACCESS_KEY_FILE="$work/root/access_key" RUSTFS_SECRET_KEY_FILE="$work/root/secret_key" \
  RUSTFS_REGION=us-east-1 RUSTFS_CONSOLE_ENABLE=false RUSTFS_CHECK_UPDATE=false RUSTFS_OBS_LOG_DIRECTORY="" \
  RUSTFS_IDENTITY_OPENID_CONFIG_URL="$issuer/.well-known/openid-configuration" \
  RUSTFS_IDENTITY_OPENID_ISSUER=https://kubernetes.default.svc.cluster.local \
  RUSTFS_IDENTITY_OPENID_CLIENT_ID=rask-object-store RUSTFS_IDENTITY_OPENID_CLAIM_NAME=sub \
  RUSTFS_IDENTITY_OPENID_HIDE_FROM_UI=true RUSTFS_OUTBOUND_ALLOW_ORIGINS="$issuer" \
  "$store" >"$work/store.log" 2>&1 &
store_pid=$!

outbox=_lineage_outbox
relay_policy='{"Version": "2012-10-17", "Statement": [
  {"Sid": "FindTheBucket", "Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": ["arn:aws:s3:::'$bucket'"]},
  {"Sid": "ListItsOwnOutbox", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::'$bucket'"], "Condition": {"StringLike": {"s3:prefix": ["'$outbox'", "'$outbox'/", "'$outbox'/*"]}}},
  {"Sid": "DrainItsOwnOutboxAndNothingElse", "Effect": "Allow", "Action": ["s3:GetObject", "s3:DeleteObject"], "Resource": ["arn:aws:s3:::'$bucket'/'$outbox'", "arn:aws:s3:::'$bucket'/'$outbox'/*"]},
  {"Sid": "ClaimItsOwnTimersWindows", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": ["arn:aws:s3:::'$bucket'/_timers/lineage-relay.json"]}]}'
producer_policy='{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"], "Resource": ["arn:aws:s3:::'$bucket'/'$outbox'/*"]}]}'
uv run --no-sync scripts/rustfs_admin.py --endpoint "$endpoint" --root-access-key rask-check-root --root-secret-file "$work/root/secret_key" \
  --bucket "$bucket" \
  --policy "system:serviceaccount:rask:rask-sa-lineage=$relay_policy" \
  --policy "system:serviceaccount:rask:rask-sa-catalog=$producer_policy" ||
  { echo "!! provisioning failed; the store's log:" >&2; tail -20 "$work/store.log" >&2; exit 1; }

for _ in $(seq 1 60); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$catalog_port/livez" && break
  sleep 1
done
curl -s -m 2 -o /dev/null "http://127.0.0.1:$catalog_port/livez" ||
  { echo "!! the catalog fixture did not come up; its log:" >&2; tail -20 "$work/catalog.log" >&2; exit 1; }

export RASK_PORT_CATALOG_URL="http://127.0.0.1:$catalog_port"
export RASK_PORT_STORE_ENDPOINT="$endpoint" RASK_PORT_STORE_BUCKET="$bucket" RASK_PORT_STORE_ISSUER="$issuer"
(cd "$root/rust" && cargo test --locked -p lineage --test lanes_live -- --nocapture --test-threads "${RASK_PORT_TEST_THREADS:-2}")
