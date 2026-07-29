#!/usr/bin/env bash
# Phase 2 live proof: a version-pinned + run-faceted merge_insert surfaces in the lineage graph, and every
# audit forgery/500 vector is rejected on the deployed image. Drives the LIVE kind `lance` cluster with a
# real Dex alice id_token (governed auth on). One self-contained process so the port-forwards stay alive.
#
# Assumes the freshly-built lance-rest-catalog:dev is already `kind load`ed + the catalog pod rolled
# (make images load && kubectl delete pod -l app.kubernetes.io/component=catalog). I mutate nothing on the
# cluster except creating alice-owned demo tables (unique per run), so run me OUTSIDE auto mode (or `!`-prefix).
#
#   bash scripts/verify_merge_lineage.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$PWD/.localbin:$PATH"
RELEASE="${RELEASE:-lance-ns}"

PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
fail() { echo "!! FAIL: $*"; exit 1; }

pkill -f "port-forward.*$RELEASE-(dex|catalog|lineage)" 2>/dev/null || true
sleep 1
kubectl port-forward "svc/$RELEASE-dex" 5556:5556     >/tmp/pf-dex.log 2>&1 & PIDS+=($!)
kubectl port-forward "svc/$RELEASE-catalog" 2333:2333 >/tmp/pf-cat.log 2>&1 & PIDS+=($!)
kubectl port-forward "svc/$RELEASE-lineage" 8010:8000 >/tmp/pf-lin.log 2>&1 & PIDS+=($!)

for i in $(seq 1 30); do
  d=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:5556/dex/.well-known/openid-configuration 2>/dev/null || true)
  c=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:2333/livez 2>/dev/null || true)
  l=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:8010/livez 2>/dev/null || true)
  [ "$d" = "200" ] && [ "$c" = "200" ] && [ "$l" = "200" ] && { echo "✓ forwards ready (dex=$d catalog=$c lineage=$l)"; break; }
  [ "$i" = "30" ] && fail "port-forwards never became ready (dex=$d catalog=$c lineage=$l)"
  sleep 1
done

mint() {
  curl -s -m10 http://localhost:5556/dex/token \
    -d grant_type=password -d client_id=lance-catalog -d client_secret=lance-catalog-secret \
    -d scope="openid email" -d username="$1" -d password=password \
  | uv run python -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"
}
ALICE="$(mint alice@example.com)"
[ -n "$ALICE" ] || fail "Dex issued no alice token (see /tmp/pf-dex.log)"
echo "✓ minted alice id_token (${#ALICE} chars)"

export CATALOG=http://localhost:2333 LINEAGE=http://localhost:8010 ALICE NS="mrg$$"
uv run scripts/verify_merge_lineage.py
