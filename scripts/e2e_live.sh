#!/usr/bin/env bash
# Run the e2e suites against the LIVE estate — the deployed k3s release, not a throwaway cluster.
#
# WHY THIS EXISTS. 111 e2e test functions across 30 files gate the estate's headline behaviour, and
# nothing ran them against what is deployed: `make test` excludes the marker (`-m "not e2e"`), and
# `scripts/e2e_stack.sh` builds its own kind cluster with a reduced feature set (no observability, no
# maintenance, no web) precisely so a GitHub runner can hold it. So every "verified live" claim in this
# repo rested on a manual terminal session — reproducible only by the person who ran it, and by nobody
# tomorrow. This makes the live estate a target, so a regression is a failing suite instead of a memory.
#
# NOT A REPLACEMENT for `e2e_stack.sh`. That script OWNS its cluster: it installs, seeds, asserts and
# tears down, which is what CI needs. This one owns NOTHING — it discovers a running release, derives
# every address and credential from it, and runs read-mostly suites against it. The two answer
# different questions: "does a fresh install work" and "does the thing we are running work".
#
# EVERY VALUE IS DISCOVERED, never hardcoded. ClusterIPs move on every re-deploy, and a script holding
# yesterday's address fails in a way that reads as a broken service. The one input is the release name.
#
# Usage:  bash scripts/e2e_live.sh [-m MARKER | FILE...]
#         RELEASE=rask KUBECONFIG=/etc/rancher/k3s/k3s.yaml bash scripts/e2e_live.sh -m cas
set -euo pipefail

RELEASE="${RELEASE:-rask}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.localbin"
export PATH="$BIN:$PATH"

step() { printf '\n\033[1m>> %s\033[0m\n' "$*"; }
fail() { printf '\033[31m!! %s\033[0m\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || fail "kubectl is not on PATH"
kubectl get ns >/dev/null 2>&1 || fail "no reachable cluster (KUBECONFIG=$KUBECONFIG)"

# ---- discovery ---------------------------------------------------------------------------------
# ClusterIP rather than a port-forward: these suites run from the host, the k3s service network is
# routable here, and a forward per service is eleven background processes to leak.
svc() {
  local name="$1" ip port
  ip="$(kubectl get svc "$name" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
  port="$(kubectl get svc "$name" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
  [ -n "$ip" ] && [ -n "$port" ] && printf '%s:%s' "$ip" "$port"
}

step "1/4 discovering the deployed release"
CATALOG="$(svc "$RELEASE-catalog")"  || true
LINEAGE="$(svc "$RELEASE-lineage")"  || true
GATEWAY="$(svc "$RELEASE-gateway")"  || true
S3="$(svc "$RELEASE-rustfs-io")"     || true
DEX="$(svc "$RELEASE-dex")"          || true
FGA="$(svc "$RELEASE-openfga")"      || true
GREPTIME="$(svc "$RELEASE-greptimedb-standalone")" || true
MAINT="$(svc "$RELEASE-maintenance")" || true
[ -n "$CATALOG" ] || fail "no $RELEASE-catalog service — is the release deployed?"
printf '   catalog=%s lineage=%s gateway=%s s3=%s\n' "$CATALOG" "$LINEAGE" "$GATEWAY" "$S3"

step "2/4 minting a real bearer through Dex"
# The estate is governed, so an unauthenticated suite proves nothing. The password grant is the same
# one `e2e_stack.sh` uses; the static client and users come from the deployed configmap, so this
# follows the estate rather than assuming it.
DEX_CLIENT="$(kubectl get cm "$RELEASE-dex" -o jsonpath='{.data.config\.yaml}' 2>/dev/null | sed -n 's/^ *- id: *//p' | head -1)"
DEX_SECRET="$(kubectl get cm "$RELEASE-dex" -o jsonpath='{.data.config\.yaml}' 2>/dev/null | sed -n 's/^ *secret: *//p' | head -1)"
: "${DEX_CLIENT:=lance-catalog}" ; : "${DEX_SECRET:=lance-catalog-secret}"
mint() {
  curl -s -m 20 "http://$DEX/dex/token" -d grant_type=password -d client_id="$DEX_CLIENT" \
    -d client_secret="$DEX_SECRET" -d scope="openid email" -d username="$1" -d password=password \
    | uv run python -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"
}
ALICE="$(mint alice@example.com)"; BOB="$(mint bob@example.com)"
[ -n "$ALICE" ] || fail "Dex issued no token for alice — is auth enabled and dex reachable at $DEX?"
printf '   alice=%s… bob=%s…\n' "${ALICE:0:18}" "${BOB:0:18}"

step "3/4 reading the estate's own secrets"
# From the Dapr app-token Secret, never from a values file: the token the sidecar stamps is the one
# the doors verify, and any other copy is a guess.
DAPR_TOKEN="$(kubectl get secret "$RELEASE-dapr-app-token" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
S3_KEY="$(kubectl get secret "$RELEASE-rustfs" -o jsonpath='{.data.accesskey}' 2>/dev/null | base64 -d || true)"
S3_SECRET="$(kubectl get secret "$RELEASE-rustfs" -o jsonpath='{.data.secretkey}' 2>/dev/null | base64 -d || true)"

export LANCE_E2E_CATALOG_URL="http://$CATALOG"
export LANCE_E2E_LINEAGE_URL="http://$LINEAGE"
export LANCE_E2E_GATEWAY_URL="http://$GATEWAY"
export LANCE_E2E_S3="http://$S3"
export LANCE_E2E_S3_ENDPOINT="http://$S3"
export LANCE_E2E_DEX="http://$DEX/dex"
export LANCE_E2E_FGA="http://$FGA"
export LANCE_E2E_GREPTIME_URL="http://$GREPTIME"
export LANCE_E2E_MAINTENANCE_URL="http://$MAINT"
export LANCE_E2E_TOKEN="$ALICE"
export LANCE_E2E_ADMIN_TOKEN="$ALICE"
# NOT exported blind. `test_create_warehouse_denied_for_non_admin` asserts a 403, and on THIS estate
# bob is a member of `team:eng` which is bound to `project:acme` — and `project.admin` is
# "[user, role#assignee] or member from team", so bob is an admin and the create succeeds. The suite
# then fails on a false premise and reads as a governance hole. It is not one; the identity was wrong.
# So the token is offered only when the estate agrees it is non-admin, and the suite SKIPS otherwise —
# an honest skip beats a red test that alleges something untrue.
if [ -n "${BOB:-}" ] && [ -n "$FGA" ]; then
  BOB_SUB="$(BOB="$BOB" uv run python -c "
import base64, json, os
b = os.environ['BOB'].split('.')[1]; b += '=' * (-len(b) % 4)
print(json.loads(base64.urlsafe_b64decode(b))['sub'])" 2>/dev/null || true)"
  STORE_ID="$(curl -s -m 10 "http://$FGA/stores" | uv run python -c "
import sys, json
print((json.load(sys.stdin).get('stores') or [{}])[0].get('id',''))" 2>/dev/null || true)"
  if [ -n "$BOB_SUB" ] && [ -n "$STORE_ID" ]; then
    ADMIN="$(curl -s -m 10 -X POST "http://$FGA/stores/$STORE_ID/check" -H 'content-type: application/json' \
      -d "{\"tuple_key\":{\"user\":\"user:$BOB_SUB\",\"relation\":\"can_administer\",\"object\":\"project:${LANCE_E2E_PROJECT:-acme}\"}}" \
      | uv run python -c "import sys,json;print(json.load(sys.stdin).get('allowed'))" 2>/dev/null || true)"
    if [ "$ADMIN" = "False" ]; then
      export LANCE_E2E_NONADMIN_TOKEN="$BOB"
    else
      printf '   note: bob holds can_administer on %s here, so the non-admin legs SKIP rather than fail falsely\n' "${LANCE_E2E_PROJECT:-acme}"
    fi
  fi
fi
export LANCE_E2E_DAPR_TOKEN="$DAPR_TOKEN"
export LANCE_E2E_OIDC_CLIENT_ID="$DEX_CLIENT"
export LANCE_E2E_OIDC_CLIENT_SECRET="$DEX_SECRET"
export LANCE_E2E_DEX_SECRET="$DEX_SECRET"
export LANCE_E2E_PROJECT="${LANCE_E2E_PROJECT:-acme}"
export LANCE_E2E_DELIM='$'
export LANCE_E2E_RELEASE="$RELEASE"
export LANCE_E2E_OUTBOX_URI="s3://lance-catalog/_lineage_outbox"
export LANCE_E2E_RECONCILE_BINDING=lineage-reconcile-cron
export LANCE_E2E_MAINTENANCE_BINDING=maintenance-cron
export AWS_ACCESS_KEY_ID="$S3_KEY"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET"

step "4/4 running the suites against the live estate"
# `-m e2e` and NOT a file list: the marker is what the suites declare about themselves, so a new suite
# is picked up without editing this script. Args override for a narrower run.
if [ "$#" -gt 0 ]; then
  PYTHONPATH=services uv run pytest "$@" -v
else
  PYTHONPATH=services uv run pytest tests/e2e-py -m e2e -v
fi
