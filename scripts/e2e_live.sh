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
# THE NAMED `http` PORT, NOT `ports[0]`. OpenFGA publishes grpc=8081 FIRST and http=8080 second, so
# the positional pick handed every suite the gRPC port: each one probes `{FGA}/healthz` and got a
# connection that speaks HTTP/2 frames, which reads as "openfga not reachable". It also silently
# broke this script's OWN `can_administer` probe below — the check that decides whether bob's token
# is safe to offer — so the non-admin legs skipped for a reason that had nothing to do with bob.
# Measured 2026-09-06: rask-openfga ports are grpc=8081, http=8080, playground=3000, metrics=2112.
svc() {
  local name="$1" ip port
  ip="$(kubectl get svc "$name" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
  port="$(kubectl get svc "$name" -o jsonpath='{.spec.ports[?(@.name=="http")].port}' 2>/dev/null || true)"
  [ -n "$port" ] || port="$(kubectl get svc "$name" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
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
# The producer answers to a LEGACY name in the suites. `LANCE_E2E_LANCERAY_URL` predates the rename
# and every consumer probes it as `("medallion-producer", LANCERAY)` — so the variable names the
# medallion producer, not anything on the Ray plane. Nine legs skipped for want of this one line.
MEDALLION="$(svc "$RELEASE-medallion-producer")" || true
# One mover is enough: the quality-block drive addresses whichever it is given, and bronze-to-silver
# is the lane the cascade always has.
MOVER="$(svc "$RELEASE-bronze-to-silver")" || true
AGE="$(svc "$RELEASE-age")" || true
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
# A THIRD IDENTITY, because bob is not the non-admin this estate needs. `project.admin` is
# "[user, role#assignee] or member from team" and bob is in `team:eng`, which is bound to
# `project:acme` — so bob holds `can_administer` and the 403 legs had nothing to assert.
# `publisher@rask.internal` is in the deployed Dex configmap and is bound to no team; measured
# 2026-09-06: can_administer(project:acme) is False for publisher and True for bob.
PUBLISHER="$(mint publisher@rask.internal)"
[ -n "$ALICE" ] || fail "Dex issued no token for alice — is auth enabled and dex reachable at $DEX?"
printf '   alice=%s… bob=%s…\n' "${ALICE:0:18}" "${BOB:0:18}"

step "3/4 reading the estate's own secrets"
# From the Dapr app-token Secret, never from a values file: the token the sidecar stamps is the one
# the doors verify, and any other copy is a guess.
DAPR_TOKEN="$(kubectl get secret "$RELEASE-dapr-app-token" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
S3_KEY="$(kubectl get secret "$RELEASE-rustfs" -o jsonpath='{.data.accesskey}' 2>/dev/null | base64 -d || true)"
S3_SECRET="$(kubectl get secret "$RELEASE-rustfs" -o jsonpath='{.data.secretkey}' 2>/dev/null | base64 -d || true)"
# The lineage suites open the AGE graph DIRECTLY rather than through the service, because what they
# assert is that the write reached the store — an assertion the service's own read cannot make. The
# deployment's `LINEAGE_DATABASE_URL` carries no password (the pod takes it from the Dapr secret
# store at lifespan), so the host-run form is rebuilt here from the same secret the cluster uses.
PG_PASSWORD="$(kubectl get secret "$RELEASE-infra-credentials" -o jsonpath='{.data.postgres-password}' 2>/dev/null | base64 -d || true)"

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
if [ -n "$FGA" ]; then
  STORE_ID="$(curl -s -m 10 "http://$FGA/stores" | uv run python -c "
import sys, json
print((json.load(sys.stdin).get('stores') or [{}])[0].get('id',''))" 2>/dev/null || true)"
  sub_of() { TOK="$1" uv run python -c "
import base64, json, os
b = os.environ['TOK'].split('.')[1]; b += '=' * (-len(b) % 4)
print(json.loads(base64.urlsafe_b64decode(b))['sub'])" 2>/dev/null || true; }
  can_administer() {
    curl -s -m 10 -X POST "http://$FGA/stores/$STORE_ID/check" -H 'content-type: application/json' \
      -d "{\"tuple_key\":{\"user\":\"user:$1\",\"relation\":\"can_administer\",\"object\":\"project:${LANCE_E2E_PROJECT:-acme}\"}}" \
      | uv run python -c "import sys,json;print(json.load(sys.stdin).get('allowed'))" 2>/dev/null || true
  }
  # ASK THE ESTATE WHICH IDENTITY IS ACTUALLY NON-ADMIN rather than naming one. The suite asserts a
  # 403, so offering a token that turns out to hold `can_administer` makes it fail on a false premise
  # and read as a governance hole — which is exactly what happened with bob. Candidates in the
  # deployed Dex configmap are tried in order and the first genuine non-admin wins.
  if [ -n "$STORE_ID" ]; then
    for CAND_NAME in publisher bob; do
      eval "CAND_TOK=\${$(echo "$CAND_NAME" | tr '[:lower:]' '[:upper:]'):-}"
      [ -n "$CAND_TOK" ] || continue
      CAND_SUB="$(sub_of "$CAND_TOK")"
      [ -n "$CAND_SUB" ] || continue
      if [ "$(can_administer "$CAND_SUB")" = "False" ]; then
        export LANCE_E2E_NONADMIN_TOKEN="$CAND_TOK"
        printf '   non-admin identity: %s (can_administer on %s is False)\n' "$CAND_NAME" "${LANCE_E2E_PROJECT:-acme}"
        break
      fi
    done
    [ -n "${LANCE_E2E_NONADMIN_TOKEN:-}" ] || printf '   note: every candidate holds can_administer on %s — the non-admin legs SKIP rather than fail falsely\n' "${LANCE_E2E_PROJECT:-acme}"
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
# THE S3 PAIR IS READ UNDER ITS OWN NAMES TOO, and exporting only the AWS_* spelling is why suites
# that reach the store directly reported it UNREACHABLE rather than skipping honestly: they read
# `LANCE_E2E_S3_ACCESS_KEY`/`_SECRET_KEY`, whose defaults are `rustfsadmin`/`rustfsadmin` — a valid
# key with the wrong secret, so every request signed and every request was refused.
export LANCE_E2E_S3_ACCESS_KEY="$S3_KEY"
export LANCE_E2E_S3_SECRET_KEY="$S3_SECRET"
export LANCE_E2E_S3_BUCKET="${LANCE_E2E_S3_BUCKET:-lance-catalog}"
export LANCE_E2E_S3_REGION="${LANCE_E2E_S3_REGION:-us-east-1}"

# ONE ADDRESS, SEVERAL NAMES. These are not aliases kept for compatibility — each suite was written
# against a different bring-up (a port-forward, `e2e_stack.sh`, a local compose) and named the same
# service for what that bring-up called it. The runner's job is to answer to all of them rather than
# to make thirty suites agree on a spelling.
[ -n "$MEDALLION" ] && export LANCE_E2E_LANCERAY_URL="http://$MEDALLION"
[ -n "$MOVER" ] && export LANCE_E2E_MOVER_URL="http://$MOVER"
[ -n "$MOVER" ] && export LANCE_E2E_MOVER_TOKEN="$DAPR_TOKEN"
export LANCE_E2E_AUTH_SERVER="http://$CATALOG"
export MEDIA_CATALOG_URL="http://$CATALOG"
export LANCE_REST_E2E_URL="http://$CATALOG"
if [ -n "$AGE" ] && [ -n "$PG_PASSWORD" ]; then
  export LINEAGE_DATABASE_URL="postgresql://lance:$PG_PASSWORD@$AGE/lineage"
else
  printf '   note: no %s-age service or no postgres-password — the 10 direct-AGE legs will SKIP\n' "$RELEASE"
fi

step "4/4 running the suites against the live estate"
# `-m e2e` and NOT a file list: the marker is what the suites declare about themselves, so a new suite
# is picked up without editing this script. Args override for a narrower run.
if [ "$#" -gt 0 ]; then
  PYTHONPATH=services uv run pytest "$@" -v
else
  PYTHONPATH=services uv run pytest tests/e2e-py -m e2e -v
fi
