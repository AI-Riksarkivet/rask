#!/usr/bin/env bash
# #64 live proof of the /produce + /train HUMAN trigger door (dual-auth authorize_produce) + #83 DLQ ops.
#
# Applies the chart change (medallion OIDC env + web MEDALLION_API) to the live kind `lance` cluster, rolls
# the already-loaded lance-ray + web + catalog + lineage images (all backend shares lance-rest-catalog:dev,
# so this one deploy also ships #68 + Phase A + #83), then drives the door end-to-end against a real Dex token:
#   • a project ADMIN bearer (alice, admin project:acme)      → 202  (the new human door opens)
#   • a NON-admin bearer     (bob, no grant)                  → 403  (FGA denies, not a silent allow)
#   • the service app-token  (dapr-api-token)                 → 202  (the unchanged service path)
#   • NO credential                                           → 403  (fail-closed)
#
# The images must already be built + `kind load`ed (lance-rest-catalog:dev, lance-lineage-web:dev). Run me
# from the repo root. I mutate the shared cluster (helm upgrade + pod delete), so run me OUTSIDE auto mode
# (or `!`-prefix in the session) so the permission prompt is shown.
set -euo pipefail

RELEASE="${RELEASE:-lance-ns}"
export PATH="$PWD/.localbin:$PATH"
PF_PIDS=()
cleanup() { for pid in "${PF_PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT
step() { echo; echo "━━ $* ━━"; }
fail() { echo "!! FAIL: $*"; exit 1; }

step "1/7 apply the chart change (medallion OIDC env + web MEDALLION_API)"
# --reuse-values keeps the live release's config; the one NEW key is set explicitly (a new values key would
# render EMPTY under --reuse-values — the documented helm gotcha, and here it would blank the admin project).
helm upgrade "$RELEASE" ./chart --reuse-values --set medallion.produceAdminProject=acme --timeout 200s

step "2/7 roll the freshly-loaded images (kind same-tag → delete pods, not just rollout)"
# catalog too: it carries the #68 access-simulator fix (services/common/fga.py qualify flag) under the same
# :dev tag, so without an explicit pod delete the running catalog keeps the old double-prefix Check.
kubectl delete pod -l app.kubernetes.io/component=lance-ray --wait=false
kubectl delete pod -l app.kubernetes.io/component=catalog --wait=false
# lineage too: the SAME :dev image carries #83's /admin/dlq ops endpoints (all backend services share
# lance-rest-catalog:dev), so without rolling lineage the DLQ panel 404s against the old pod.
kubectl delete pod -l app.kubernetes.io/component=lineage --wait=false
# (the monolithic web app is retired — the MFE zones are web-<name> and none consumes MEDALLION_API,
# so only the backends roll here)
kubectl rollout status "deploy/$RELEASE-lance-ray" --timeout=150s
kubectl rollout status "deploy/$RELEASE-catalog" --timeout=150s
kubectl rollout status "deploy/$RELEASE-lineage" --timeout=150s

step "3/7 confirm the OIDC door env is live on lance-ray"
env_val() {
  kubectl get deploy "$RELEASE-lance-ray" \
    -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='$1')].value}"
}
[ "$(env_val MEDALLION_OIDC_ENABLED)" = "true" ] || fail "MEDALLION_OIDC_ENABLED not true on lance-ray"
[ "$(env_val MEDALLION_PRODUCE_ADMIN_PROJECT)" = "acme" ] || fail "produce-admin project not acme"
echo "   OIDC_ENABLED=true  ADMIN_PROJECT=acme  ISSUER=$(env_val MEDALLION_OIDC_ISSUER)"
# The monolithic web app is retired — every MFE zone carries MEDALLION_API (the produce/train door UI
# lives in the models zone; the env is shared zone plumbing). Assert on the models zone.
webapi="$(kubectl get deploy "$RELEASE-web-models" -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='MEDALLION_API')].value}")"
[ -n "$webapi" ] || fail "web-models has no MEDALLION_API"
echo "   web-models MEDALLION_API=$webapi"

step "4/7 port-forward dex + lance-ray + openfga + lineage"
kubectl port-forward "svc/$RELEASE-dex" 5556:5556 >/tmp/pf-dex.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-lance-ray" 8000:8000 >/tmp/pf-ray.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-openfga" 8081:8080 >/tmp/pf-fga.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-lineage" 8010:8000 >/tmp/pf-lin.log 2>&1 & PF_PIDS+=($!)
for i in $(seq 1 30); do
  d=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:5556/dex/.well-known/openid-configuration 2>/dev/null || true)
  r=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:8000/livez 2>/dev/null || true)
  [ "$d" = "200" ] && [ "$r" = "200" ] && break
  [ "$i" = "30" ] && fail "port-forwards never became ready (dex=$d ray=$r)"
  sleep 1
done

step "5/7 mint Dex tokens + seed the admin grant"
mint() {
  curl -s -m10 http://localhost:5556/dex/token \
    -d grant_type=password -d client_id=lance-catalog -d client_secret=lance-catalog-secret \
    -d scope="openid email" -d username="$1" -d password=password \
  | uv run python -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"
}
ALICE="$(mint alice@example.com)"; BOB="$(mint bob@example.com)"
[ -n "$ALICE" ] && [ -n "$BOB" ] || fail "Dex issued no token"
SUB="$(ALICE="$ALICE" uv run python -c "
import os,base64,json
p=os.environ['ALICE'].split('.')[1]; p+='='*(-len(p)%4)
print(json.loads(base64.urlsafe_b64decode(p))['sub'])")"
SID="$(fga store list --api-url http://localhost:8081 \
  | uv run python -c "import sys,json;s=[x for x in json.load(sys.stdin)['stores'] if x['name']=='lance-catalog'];print(max(s,key=lambda x:x['created_at'])['id'])")"
fga tuple write --api-url http://localhost:8081 --store-id "$SID" "user:$SUB" admin project:acme >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  a="$(fga query check --api-url http://localhost:8081 --store-id "$SID" "user:$SUB" can_administer project:acme 2>/dev/null \
      | uv run python -c "import sys,json;print(json.load(sys.stdin).get('allowed',False))" 2>/dev/null || echo False)"
  [ "$a" = "True" ] && break
  [ "$i" = "30" ] && fail "admin grant never became readable"
  sleep 1
done
echo "   user:${SUB:0:12}… is admin on project:acme"
DAPR_TOKEN="$(kubectl get secret "$RELEASE-dapr-app-token" -o jsonpath='{.data.token}' | base64 -d)"

step "6/7 drive /produce through every door"
code() { curl -s -o /dev/null -w '%{http_code}' -m15 -X POST http://localhost:8000/produce "$@"; }
admin=$(code -H "authorization: Bearer $ALICE")
nonadmin=$(code -H "authorization: Bearer $BOB")
svc=$(code -H "dapr-api-token: $DAPR_TOKEN")
none=$(code)
echo "   admin bearer (alice)   → $admin   (expect 202; 503 = auth passed, publish failed)"
echo "   non-admin bearer (bob) → $nonadmin   (expect 403)"
echo "   service app-token      → $svc   (expect 202)"
echo "   no credential          → $none   (expect 403)"
# The gate is what authorize_produce owns: an OPENED door is 202 (or a downstream-publish 503) — never the
# 401/403 a denied caller gets. A denied door is exactly 403.
opened() { [ "$1" = "202" ] || [ "$1" = "503" ]; }
opened "$admin"        || fail "admin bearer did not open the door (got $admin)"
[ "$nonadmin" = "403" ] || fail "non-admin was not denied (got $nonadmin)"
opened "$svc"          || fail "service token path regressed (got $svc)"
[ "$none" = "403" ]     || fail "missing credential was not fail-closed (got $none)"

echo; echo "✓ produce door PROVEN: admin OIDC opens · non-admin 403 · service opens · anon 403"

step "7/7 prove #83 DLQ ops endpoints are live + fail-closed (lineage)"
# The new /admin/dlq surface ships in the SAME image on the lineage pod. A 404 here means the lineage pod is
# still the OLD image (the roll didn't take); 401 without a session proves the endpoint exists + fails closed;
# 200 for a signed-in user proves the governed read works (empty backlog is fine — depth 0, still 200).
dlq() { curl -s -o /dev/null -w '%{http_code}' -m10 http://localhost:8010/admin/dlq "$@"; }
dlq_noauth=$(dlq)
dlq_alice=$(dlq -H "authorization: Bearer $ALICE")
echo "   GET /admin/dlq  no cred → $dlq_noauth   (expect 401; 404 = stale lineage image)"
echo "   GET /admin/dlq  alice   → $dlq_alice   (expect 200; governed view)"
[ "$dlq_noauth" = "401" ] || fail "#83 DLQ not live / not fail-closed (got $dlq_noauth — 404 = stale image)"
[ "$dlq_alice" = "200" ] || fail "#83 DLQ governed read failed for a signed-in user (got $dlq_alice)"
echo; echo "✓ #83 DLQ ops PROVEN: /admin/dlq live · 401 without a session · 200 for a signed-in user"
