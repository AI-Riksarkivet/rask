#!/usr/bin/env bash
# lifecycle_on_rustfs_check.sh: run the table lifecycle's, the namespace cascade's, the stock create's, the history
# fence's, the keyed commit's and a table's maintenance pass's race tests (ADR 0227, ADR 0233, ADR 0240, ADR 0243), and
# the store's refusal of a vended write credential's put over a committed file, with the commits that name a file the
# store does not hold, a transaction put over while judged, a long branch's vend and a seal the store refuses (ADR 0242),
# with the catalog's root and control root on a REAL S3 store, so every fence, claim and record is arbitrated by the
# store's own conditional writes (put-if-not-exists, If-Match) rather than the local root's file lock.
#
# Starts the pinned RustFS (scripts/rustfs_server.sh) as a process, with a root pair made for this run and read from
# files, and runs the tests' `s3` parametrization (tests/integration/conftest.py::race_ns_client and race_catalog), each test in a bucket
# of its own. The catalog writes under that root pair: what is checked is how the store arbitrates the writes, not whose
# identity makes them (the governance-chain lane checks that). The stock client's cases write with the credential the
# catalog vends them and no other key: a session the store's STS issues, under the catalog's session policy, to a user of
# the run's own holding the catalog's canned policy as the chart renders it (race_catalog); the maintain vend's test writes under the credential
# the catalog's own session policy vends. Stops the store at the end; KEEP_UP=1 leaves it running
# and prints the environment a test run needs.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
port="${RASK_LIFECYCLE_STORE_PORT:-$("$root/scripts/free_port.sh")}"
endpoint="http://127.0.0.1:$port"
work="$(mktemp -d)"
store="$(bash "$root/scripts/rustfs_server.sh")"
# The vending user holds the catalog's canned policy as the chart renders it (helm template, its subcharts from
# `make k3s-deps`), so the seal's Deny the store applies is the chart's own (ADR 0242): no helm, no lane.
command -v helm >/dev/null || { echo "!! helm not on PATH: the lane renders the catalog's store policy from the chart" >&2; exit 1; }
ls "$root"/chart/charts/*.tgz >/dev/null 2>&1 || { echo "!! chart/charts holds no subchart: run make k3s-deps" >&2; exit 1; }

cd "$root"
stop() {
  kill "${store_pid:-}" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$work"
}
[ "${KEEP_UP:-}" = 1 ] || trap stop EXIT

mkdir -p "$work/data" "$work/root"
echo rask-lifecycle-root >"$work/root/access_key"
head -c 24 /dev/urandom | base64 | tr -d '/+=' >"$work/root/secret_key"

env -i PATH="$PATH" HOME="$work" \
  RUSTFS_VOLUMES="$work/data" RUSTFS_ADDRESS="127.0.0.1:$port" \
  RUSTFS_ACCESS_KEY_FILE="$work/root/access_key" RUSTFS_SECRET_KEY_FILE="$work/root/secret_key" \
  RUSTFS_REGION=us-east-1 RUSTFS_CONSOLE_ENABLE=false RUSTFS_CHECK_UPDATE=false RUSTFS_OBS_LOG_DIRECTORY="" \
  "$store" >"$work/store.log" 2>&1 &
store_pid=$!
for _ in $(seq 1 60); do
  # Any HTTP answer means the store listens; an unsigned request is answered 403.
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$endpoint/" || true)" != 000 ] && break
  sleep 1
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "$endpoint/" || true)" != 000 ] || { echo "!! RustFS did not start; its log:" >&2; tail -20 "$work/store.log" >&2; exit 1; }
echo "RustFS $("$store" --version 2>/dev/null | head -1 || echo 1.0.3) on $endpoint"

export RASK_LIFECYCLE_STORE_ENDPOINT="$endpoint" RASK_LIFECYCLE_STORE_ACCESS_KEY=rask-lifecycle-root
RASK_LIFECYCLE_STORE_SECRET_KEY="$(cat "$work/root/secret_key")"
export RASK_LIFECYCLE_STORE_SECRET_KEY
if [ "${KEEP_UP:-}" = 1 ]; then
  echo "RASK_LIFECYCLE_STORE_ENDPOINT=$endpoint RASK_LIFECYCLE_STORE_ACCESS_KEY=rask-lifecycle-root RASK_LIFECYCLE_STORE_SECRET_KEY=$RASK_LIFECYCLE_STORE_SECRET_KEY (pid $store_pid, $work)"
  exit 0
fi
uv run --no-sync pytest \
  tests/integration/test_a_lifecycle_door_keeps_what_it_answered_beside_a_concurrent_door.py \
  tests/integration/test_a_cascade_keeps_what_it_answered.py \
  tests/integration/test_a_stock_create_and_a_catalog_create_of_one_name_never_both_land.py \
  tests/integration/test_a_tables_ref_writes_share_its_history.py \
  tests/integration/test_the_store_refuses_a_put_over_a_committed_file.py \
  packages/lakehouse/tests/test_a_tables_history_is_held_by_one_kind_of_work_at_a_time.py \
  tests/integration/test_a_keyed_write_lands_once_beside_its_resend.py \
  -k s3 -p no:cacheprovider -n "${PYTEST_WORKERS:-2}" -rA "$@"
# The maintain vend's own credential, which only this lane's store can mint (ADR 0233 §9): every case is the store's.
uv run --no-sync pytest tests/integration/test_a_maintain_vend_plants_nothing_the_fence_trusts.py -p no:cacheprovider -rA "$@"
# A table's pass beside the doors on its history (ADR 0243), its deletes held or refused on the wire in front of the store.
uv run --no-sync pytest tests/integration/test_a_tables_pass_deletes_nothing_a_door_answered_for.py -k rustfs -p no:cacheprovider -rA "$@"
