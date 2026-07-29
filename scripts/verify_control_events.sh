#!/usr/bin/env bash
# Live proof (kind `lance`) of the control-plane change-event pipeline, end to end:
#   a governance mutation → best-effort emit → DEDICATED broadcast Dapr topic (catalog.control.v1, no
#   queueGroupName) → every replica's ring buffer → GET /v1/events → the admin console's query.live feed.
# Asserts the whole chain by driving the REAL catalog with real OIDC tokens + FGA tuples:
#   - alice (ESTATE admin = owner of the root warehouse, fga_root_object) → GET /v1/events 200, and after she
#     creates a namespace the poll returns a namespace_created event whose actor is her verified subject;
#   - bob (a PROJECT admin, NOT an estate admin) → 403 (the #12 fix: authz scope == data scope);
#   - anon (no bearer) → 401.
#
# Prereq: kind `lance` up with the stack deployed control-on + auth-on + fga-on, e.g.
#   make frontend-load
#   helm upgrade --install lance-ns ./chart --set auth.enabled=true --set medallion.enabled=true \
#     --set catalog.warehouses.enabled=true --set dapr.enabled=true --set dapr.sidecars=true
#   kubectl rollout restart deploy/lance-ns-dex deploy/lance-ns-lance-ray
# catalog.controlEmit defaults ON, so the broadcast component + NATS stream + subscription render. This
# script port-forwards read-only; run it OUTSIDE auto mode (or `!`-prefix the invocation).
#
# Alice's estate-admin grant is ASSERTED, not seeded (goal cond 4): the chart's auth.bootstrapAdmin
# post-install hook Job owns that grant now (deploy with --set auth.bootstrapAdmin=<alice's Dex sub>).
# Pass --seed for legacy runs to seed it manually via the fga CLI. Bob's project-admin tuple is a pure
# negative-test fixture (a PROJECT admin must 403 on the estate feed) and is seeded in both modes.
#
#   bash scripts/verify_control_events.sh [--seed]
set -uo pipefail
SEED=0
for arg in "$@"; do
  case "$arg" in
    --seed) SEED=1 ;;
    *) echo "usage: $0 [--seed]"; exit 2 ;;
  esac
done
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE="${RELEASE:-lance-ns}"
CAT_PORT="${CAT_PORT:-2333}"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
fail() { echo "!! FAIL: $*"; exit 1; }
ok() { echo "OK: $*"; }

# Port-forward the catalog (serves /v1/events + the mutation endpoints), Dex (mint user tokens), OpenFGA
# (seed the estate-admin + project-admin tuples).
kubectl port-forward "svc/$RELEASE-catalog" "$CAT_PORT:2333" >/tmp/pf-cat.log 2>&1 & PIDS+=($!)
kubectl port-forward "svc/$RELEASE-dex" 5556:5556 >/tmp/pf-dex.log 2>&1 & PIDS+=($!)
kubectl port-forward "svc/$RELEASE-openfga" 8081:8080 >/tmp/pf-fga.log 2>&1 & PIDS+=($!)
for i in $(seq 1 30); do
  c=$(curl -s -o /dev/null -w '%{http_code}' -m2 "http://localhost:$CAT_PORT/livez" 2>/dev/null || true)
  d=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:5556/dex/.well-known/openid-configuration 2>/dev/null || true)
  [ "$c" = "200" ] && [ "$d" = "200" ] && break
  [ "$i" = "30" ] && { echo "catalog=$c dex=$d"; cat /tmp/pf-cat.log /tmp/pf-dex.log; fail "port-forwards never became ready"; }
  sleep 1
done
ok "catalog + dex + openfga reachable"

# Mint a user's Dex id_token (password grant) and decode its `sub` (the verified subject the catalog stamps).
mint() { curl -s http://localhost:5556/dex/token -d grant_type=password -d client_id=lance-catalog \
  -d client_secret=lance-catalog-secret -d scope=openid -d username="$1" -d password=password | \
  python3 -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"; }
sub_of() { python3 -c "import sys,base64,json;t=sys.argv[1].split('.')[1];t+='='*(-len(t)%4);print(json.loads(base64.urlsafe_b64decode(t)).get('sub',''))" "$1"; }

ALICE="$(mint alice@example.com)"; [ -n "$ALICE" ] || fail "could not mint alice's Dex token (is dex ready?)"
BOB="$(mint bob@example.com)"; [ -n "$BOB" ] || fail "could not mint bob's Dex token"
ALICE_SUB="$(sub_of "$ALICE")"; BOB_SUB="$(sub_of "$BOB")"
[ -n "$ALICE_SUB" ] && [ -n "$BOB_SUB" ] || fail "could not decode subs"

# Estate-admin grant: ASSERT by default — the chart's auth.bootstrapAdmin hook Job owns seeding alice's
# `owner warehouse:lance_catalog` now (cond 4); --seed writes it manually for legacy stacks. Bob's
# project-admin tuple stays script-seeded in BOTH modes: it is a negative-test fixture (a mere project
# admin must 403 on the estate feed), not part of the estate bootstrap. ROOT = fga_root_object default.
SID="$(fga store list --api-url http://localhost:8081 2>/dev/null | python3 -c "import sys,json;s=json.load(sys.stdin).get('stores',[]);print(s[0]['id'] if s else '')")"
[ -n "$SID" ] || fail "no OpenFGA store (is fga provisioned?)"
if [ "$SEED" = "1" ]; then
  fga tuple write --api-url http://localhost:8081 --store-id "$SID" "user:$ALICE_SUB" owner warehouse:lance_catalog >/dev/null 2>&1 || true
fi
# Bob's project is a THROWAWAY fixture project, deliberately NOT a real tenant: granting him admin on
# acme polluted the tenants panel's admin list AND broke verify_produce_door.sh's negative case (bob
# must NOT be an acme admin there — found when both scripts ran against one persisted store 2026-07-24).
fga tuple write --api-url http://localhost:8081 --store-id "$SID" "user:$BOB_SUB" admin project:ctlfixture >/dev/null 2>&1 || true
# --seed polls longer (a fresh write can lag through OpenFGA); assert mode expects a pre-existing grant.
TRIES=$([ "$SEED" = "1" ] && echo 20 || echo 5)
for i in $(seq 1 "$TRIES"); do
  a="$(fga query check --api-url http://localhost:8081 --store-id "$SID" "user:$ALICE_SUB" can_observe_events warehouse:lance_catalog 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('allowed',False))" 2>/dev/null || true)"
  [ "$a" = "True" ] && break
  if [ "$i" = "$TRIES" ]; then
    if [ "$SEED" = "1" ]; then
      fail "alice never became an estate admin (can_observe_events) — OpenFGA lag or seeding failed"
    fi
    fail "alice is NOT an estate admin (can_observe_events on warehouse:lance_catalog). The chart owns this \
grant now: deploy with --set auth.bootstrapAdmin=$ALICE_SUB (the bootstrap-admin hook Job seeds it), or rerun \
with --seed for legacy manual seeding."
  fi
  sleep 1
done
if [ "$SEED" = "1" ]; then
  ok "seeded: alice=estate-admin (root owner), bob=project-admin"
else
  ok "asserted: alice=estate-admin (via auth.bootstrapAdmin), bob=project-admin (fixture)"
fi

CAT="http://localhost:$CAT_PORT"
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# 1) authz on the feed: anon 401, project-admin bob 403, estate-admin alice 200.
[ "$(code "$CAT/v1/events?since=0")" = "401" ] || fail "anon GET /v1/events expected 401"
ok "anon → 401"
[ "$(code -H "Authorization: Bearer $BOB" "$CAT/v1/events?since=0")" = "403" ] || fail "project-admin bob expected 403 (estate feed is not per-project)"
ok "project-admin bob → 403 (#12 authz scope == data scope)"
BASE="$(curl -s -H "Authorization: Bearer $ALICE" "$CAT/v1/events?since=0")"
echo "$BASE" | python3 -c "import sys,json;d=json.load(sys.stdin);assert 'cursor' in d and d['events']==[]" || fail "alice baseline GET /v1/events malformed"
CURSOR="$(echo "$BASE" | python3 -c "import sys,json;print(json.load(sys.stdin)['cursor'])")"
ok "estate-admin alice → 200 (baseline cursor=$CURSOR)"

# 2) drive a control-plane MUTATION as alice (create a top-level namespace under the root) → namespace_created.
NS="ctlverify$$"
mc="$(code -X POST -H "Authorization: Bearer $ALICE" -H 'Content-Type: application/json' -d '{"mode":null}' "$CAT/v1/namespace/$NS/create")"
[ "$mc" = "200" ] || [ "$mc" = "201" ] || fail "alice namespace create expected 2xx, got $mc"
ok "drove mutation: alice created namespace:$NS"

# 3) poll after the mutation → the event pipeline (emit → catalog.control.v1 → buffer → /v1/events) delivered
#    a namespace_created event whose ACTOR is alice's verified subject (never a body value). Poll a few times
#    (the emit is best-effort + the bus is async).
for i in $(seq 1 10); do
  P="$(curl -s -H "Authorization: Bearer $ALICE" "$CAT/v1/events?since=$CURSOR")"
  hit="$(echo "$P" | python3 -c "import sys,json;d=json.load(sys.stdin);print(any(e['action']=='namespace_created' and e['actor']=='user:$ALICE_SUB' for e in d['events']))" 2>/dev/null || echo False)"
  [ "$hit" = "True" ] && { ok "GET /v1/events delivered namespace_created by user:$ALICE_SUB (the live pipeline)"; break; }
  [ "$i" = "10" ] && { echo "$P"; fail "namespace_created never reached /v1/events (check catalog.control.v1 stream + the subscription)"; }
  sleep 2
done

echo
echo "PASS: control-plane change-events proven live — mutation → catalog.control.v1 → ring buffer → GET /v1/events,"
echo "      estate-admin gated (alice 200 / project-admin bob 403 / anon 401), actor = the verified subject."
