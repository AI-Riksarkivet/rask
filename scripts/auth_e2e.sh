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

# --- helpers ----------------------------------------------------------------
dex_token() {  # dex_token <email> -> id_token on stdout
  curl -s -X POST "$DEX/token" \
    -d grant_type=password -d client_id=lance-catalog \
    -d "username=$1" -d password=password -d scope=openid \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['id_token'])"
}

sub_of() {  # sub_of <id_token> -> sub claim on stdout
  printf '%s' "$1" | python3 -c \
    "import sys,base64,json;p=sys.stdin.read().split('.')[1];p+='='*(-len(p)%4);print(json.loads(base64.urlsafe_b64decode(p))['sub'])"
}

code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }

# Read the store/model the app provisioned at startup (we never created them).
store_model() {
  STORE=$(curl -s "$FGA/stores" | python3 -c \
    "import sys,json;print(sorted(json.load(sys.stdin)['stores'],key=lambda x:x['created_at'])[-1]['id'])")
  MODEL=$(curl -s "$FGA/stores/$STORE/authorization-models" | python3 -c \
    "import sys,json;print(json.load(sys.stdin)['authorization_models'][0]['id'])")
}

# Assert a tuple the APP must have written exists in OpenFGA (proves seeding).
assert_tuple() {  # assert_tuple <user> <relation> <object> <label>
  local found
  found=$(curl -s -X POST "$FGA/stores/$STORE/read" -H 'content-type: application/json' \
    -d "{\"tuple_key\":{\"user\":\"$1\",\"relation\":\"$2\",\"object\":\"$3\"}}" \
    | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('tuples',[])))")
  if [ "$found" -ge 1 ]; then
    echo "  ok   seeded tuple present: $1 $2 $3   ($4)"
  else
    echo "  FAIL expected app to seed tuple: $1 $2 $3   ($4)"
    exit 1
  fi
}

expect() {  # expect <want> <got> <label>
  if [ "$1" = "$2" ]; then
    echo "  ok   $3 -> $2"
  else
    echo "  FAIL $3 -> got $2, want $1"
    exit 1
  fi
}

# A 2-byte schema-only Arrow IPC stream is enough for create/insert.
arrow_stream() {  # writes an Arrow IPC stream of one int64 column to $1
  uv run --no-sync python - "$1" <<'PY'
import sys
import pyarrow as pa
table = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
with pa.OSFile(sys.argv[1], "wb") as sink:
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
PY
}

# --- run --------------------------------------------------------------------
echo "== tokens from Dex =="
ALICE=$(dex_token alice@example.com)
BOB=$(dex_token bob@example.com)
ALICE_SUB=$(sub_of "$ALICE")
BOB_SUB=$(sub_of "$BOB")
store_model
echo "  store=$STORE model=$MODEL alice=$ALICE_SUB bob=$BOB_SUB"

ALICE_H=(-H "Authorization: Bearer $ALICE")
BOB_H=(-H "Authorization: Bearer $BOB")
JSON=(-H 'content-type: application/json')

ARROW=$(mktemp --suffix=.arrows)
arrow_stream "$ARROW"

echo "== 1. no token -> 401 =="
expect 401 "$(code -X POST "$SERVER/v1/namespace/$NS/create" "${JSON[@]}" -d '{}')" "anon create namespace"

echo "== 2. alice creates namespace -> 200 (app seeds owner) =="
expect 200 "$(code -X POST "$SERVER/v1/namespace/$NS/create" "${ALICE_H[@]}" "${JSON[@]}" -d '{}')" \
  "alice create namespace"
assert_tuple "user:$ALICE_SUB" owner "$NS_OBJ" "namespace owner seeded on create"

echo "== 3. alice creates table -> 200 (owner cascades to writer; app seeds owner+parent) =="
expect 200 "$(code -X POST "$SERVER/v1/table/$TID/create" \
  "${ALICE_H[@]}" -H "content-type: application/vnd.apache.arrow.stream" \
  --data-binary "@$ARROW")" "alice create table"
assert_tuple "user:$ALICE_SUB" owner "$TBL_OBJ" "table owner seeded on create"
assert_tuple "$NS_OBJ" parent "$TBL_OBJ" "table parent link seeded on create"

echo "== 4. alice reads table -> 200 (reader cascades from owner) =="
expect 200 "$(code -X POST "$SERVER/v1/table/$TID/describe" "${ALICE_H[@]}")" "alice describe table"

echo "== 5. alice writes table -> 2xx (writer cascades from owner) =="
INS=$(code -X POST "$SERVER/v1/table/$TID/insert" \
  "${ALICE_H[@]}" -H "content-type: application/vnd.apache.arrow.stream" --data-binary "@$ARROW")
case "$INS" in
  2*) echo "  ok   alice insert table -> $INS" ;;
  *)  echo "  FAIL alice insert table -> got $INS, want 2xx"; exit 1 ;;
esac

echo "== 6. bob reads table -> 403 (no grant) =="
expect 403 "$(code -X POST "$SERVER/v1/table/$TID/describe" "${BOB_H[@]}")" "bob describe table"

echo "== 7. bob writes table -> 403 (no grant) =="
expect 403 "$(code -X POST "$SERVER/v1/table/$TID/drop" "${BOB_H[@]}")" "bob drop table"

rm -f "$ARROW"
echo "== ALL ASSERTIONS PASSED (app-side seeding verified) =="
