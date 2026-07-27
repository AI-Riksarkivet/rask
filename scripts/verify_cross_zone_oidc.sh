#!/usr/bin/env bash
# P5 live proof: a REAL per-user Dex login driven through the INGRESS (one origin, all zones), asserting
# the sealed session cookie carries ACROSS zones (sign in on /lakehouse/data → still signed in on
# /lakehouse/admin) + per-user
# authz through the zones' BFF (alice, project-admin → the produce door 2xx; bob, no grant → 403). Drives
# the LIVE kind `lance` cluster with the P5 micro-frontend zones deployed OIDC-on behind ingress-nginx.
#
# Prereq (see docs/DEPLOY.md "the cross-zone drive"): the 5 zone images built + kind-loaded, ingress-nginx
# installed, and `helm upgrade … --set auth.enabled=true --set medallion.enabled=true
# --set medallion.produceAdminProject=acme --set ingress.enabled=true --set frontend.oidc.enabled=true
# --set frontend.oidc.publicIssuer=http://lance-ns-dex:5556/dex
# --set frontend.oidc.publicOrigin=http://localhost:8090 --set frontend.oidc.sessionSecret=<48>` applied,
# then `kubectl rollout restart deploy/lance-ns-dex deploy/lance-ns-lance-ray`. This script seeds alice's
# admin grant + drives the browser (read-only forwards); run it OUTSIDE auto mode (or `!`-prefix).
#
#   bash scripts/verify_cross_zone_oidc.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$PWD/.localbin:$PATH"
RELEASE="${RELEASE:-lance-ns}"
ORIGIN_PORT="${ORIGIN_PORT:-8090}" # must equal frontend.oidc.publicOrigin's port (the registered redirect URI)
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
fail() { echo "!! FAIL: $*"; exit 1; }

# One origin = the ingress-nginx controller (path-routes every zone); Dex for the browser host-resolver rule;
# OpenFGA to seed alice's admin grant.
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller "$ORIGIN_PORT:80" >/tmp/pf-ingress.log 2>&1 & PIDS+=($!)
kubectl port-forward "svc/$RELEASE-dex" 5556:5556 >/tmp/pf-dex.log 2>&1 & PIDS+=($!)
kubectl port-forward "svc/$RELEASE-openfga" 8081:8080 >/tmp/pf-fga.log 2>&1 & PIDS+=($!)
for i in $(seq 1 30); do
  # Probe the ORIGIN ROOT, not a zone path. This polled "/data" — a path the 7 -> 4 zone merge deleted
  # (data is an area of the lakehouse zone now), so it answered 404, never matched, and the loop below
  # fail()ed after 30 tries. The one script that proves cross-zone OIDC end to end could not start, which
  # is precisely why five dead /data links reached main unnoticed. "/" is served by the catch-all zone for
  # as long as there is a frontend at all, so it cannot rot the same way.
  w=$(curl -s -o /dev/null -w '%{http_code}' -m2 "http://localhost:$ORIGIN_PORT/" 2>/dev/null || true)
  d=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:5556/dex/.well-known/openid-configuration 2>/dev/null || true)
  # Anything that is not a transport failure means the ingress is answering: 200 signed-in, 302 to the
  # login-first gate when anonymous, 307/308 on a base redirect. Pinning two exact codes is what made the
  # old probe brittle in the first place.
  case "$w" in 200|302|307|308) ready=1 ;; *) ready=0 ;; esac
  [ "$ready" = "1" ] && [ "$d" = "200" ] && { echo "✓ forwards ready (ingress/=$w dex=$d)"; break; }
  [ "$i" = "30" ] && { echo "ingress=$w dex=$d"; cat /tmp/pf-ingress.log /tmp/pf-dex.log; fail "port-forwards never became ready"; }
  sleep 1
done

# Seed alice = admin on project:acme (same grant verify_produce_door.sh uses). Mint her Dex token, decode the
# sub, write the tuple, and wait until the check is readable (OpenFGA is eventually consistent).
mint() { curl -s http://localhost:5556/dex/token -d grant_type=password -d client_id=lance-catalog \
  -d client_secret=lance-catalog-secret -d scope=openid -d username="$1" -d password=password | \
  python3 -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"; }
sub_of() { python3 -c "import sys,base64,json;t=sys.argv[1].split('.')[1];t+='='*(-len(t)%4);print(json.loads(base64.urlsafe_b64decode(t)).get('sub',''))" "$1"; }
ID="$(mint alice@example.com)"; [ -n "$ID" ] || fail "could not mint alice's Dex token (is dex ready?)"
SUB="$(sub_of "$ID")"; [ -n "$SUB" ] || fail "could not decode alice's sub"
SID="$(fga store list --api-url http://localhost:8081 2>/dev/null | python3 -c "import sys,json;s=json.load(sys.stdin).get('stores',[]);print(s[0]['id'] if s else '')")"
[ -n "$SID" ] || fail "no OpenFGA store (is auth.enabled + the model provisioned?)"
fga tuple write --api-url http://localhost:8081 --store-id "$SID" "user:$SUB" admin project:acme >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  a="$(fga query check --api-url http://localhost:8081 --store-id "$SID" "user:$SUB" can_administer project:acme 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('allowed',False))" 2>/dev/null || true)"
  [ "$a" = "True" ] && { echo "✓ alice (user:${SUB:0:12}…) is admin on project:acme"; break; }
  [ "$i" = "30" ] && fail "alice admin grant never became readable"
  sleep 1
done

# The @playwright/test browser lives in a zone's node_modules (any zone has it); run from there.
cd frontend/microfrontends/lakehouse
ORIGIN="http://localhost:$ORIGIN_PORT" node "$ROOT/scripts/verify_cross_zone_oidc.mjs"
