#!/usr/bin/env bash
# Run the e2e suites against the LIVE estate — the deployed k3s release, not a throwaway cluster.
#
# WHY THIS EXISTS. The e2e suites (80 test functions in 26 files, counted 2026-10-18) gate the estate's headline
# behaviour, and `make test` excludes their marker (`-m "not e2e"`). `scripts/e2e_stack.sh` runs every module it can
# serve on each push, against a kind cluster it installs from scratch with the telemetry chain, the zones and the
# Gateway off, so a GitHub runner can hold it. Whether the release actually deployed works is the question it cannot
# answer: this makes the live estate a target, so a regression there is a failing suite instead of a memory.
#
# DRIVEN PER DEPLOY, NOT ON A CLOCK (ADR 0167): each row's read-back runs it after a deploy, and Phase 1's soak reads a
# kept drive a day later.
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
#
# Phase 1's acceptance with criterion 5's drills, its drive kept, then the soak a day later (XC-090):
#         bash scripts/e2e_live.sh -m phase1 --require-live --chaos --keep-drive PATH
#         bash scripts/e2e_live.sh -m soak --require-live --soak PATH
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
# svc <name> [namespace]: the release's own namespace unless one is named (a fixture's, for the telemetry store).
svc() {
  local name="$1" ip port
  local ns=()
  [ -n "${2:-}" ] && ns=(--namespace "$2")
  ip="$(kubectl get svc "$name" "${ns[@]}" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
  port="$(kubectl get svc "$name" "${ns[@]}" -o jsonpath='{.spec.ports[?(@.name=="http")].port}' 2>/dev/null || true)"
  [ -n "$port" ] || port="$(kubectl get svc "$name" "${ns[@]}" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
  [ -n "$ip" ] && [ -n "$port" ] && printf '%s:%s' "$ip" "$port"
}

step "1/4 discovering the deployed release"
CATALOG="$(svc "$RELEASE-catalog")"  || true
LINEAGE="$(svc "$RELEASE-lineage")"  || true
# The edge is the cluster's Gateway: on k3s, Traefik's Service, which serves the release's HTTPRoutes.
EDGE="$(svc traefik kube-system)"   || true
# The object store fixture's Service (deploy/fixtures/rustfs). An estate on a platform's S3 has none, and the address
# stays empty rather than the literal `http://` botocore refuses with `ValueError: Invalid endpoint`.
S3="$(svc "$RELEASE-rustfs")"        || true
DEX="$(svc "$RELEASE-dex")"          || true
FGA="$(svc "$RELEASE-openfga")"      || true
# The telemetry store is the platform's; on a dev estate, the observability fixture's (deploy/fixtures/observability).
GREPTIME="$(svc greptimedb observability)" || true
MAINT="$(svc "$RELEASE-maintenance")" || true
NOTIFICATIONS="$(svc "$RELEASE-notifications")" || true
# The annotator's own release beside rask (deploy/fixtures/annotator), when it is installed.
ANNOTATOR="$(svc "$RELEASE-annotator")" || true
# The notifications service mounts its routers under the fleet's API prefix, read from the release's own config.
API_PREFIX="$(kubectl get cm "$RELEASE-config" -o jsonpath='{.data.RASK_API_PREFIX}' 2>/dev/null || true)"
# THE BUS THE RELEASE'S PUB/SUB COMPONENTS DIAL (`nats.url`), read off one of them and resolved to that Service's
# ClusterIP like every address above: the NATS fixture's `rask-nats` on a dev estate. A bus that is no Service in this
# cluster resolves to nothing, and the suites that read the bus skip.
NATS_HOST="$(kubectl get components.dapr.io lineage-pubsub -o jsonpath='{.spec.metadata[?(@.name=="natsURL")].value}' 2>/dev/null | sed -E 's#^nats://([^:/.]+).*#\1#' || true)"
NATS=""
[ -n "$NATS_HOST" ] && NATS="$(svc "$NATS_HOST")" || true
[ -n "$CATALOG" ] || fail "no $RELEASE-catalog service — is the release deployed?"
printf '   catalog=%s lineage=%s edge=%s s3=%s\n' "$CATALOG" "$LINEAGE" "$EDGE" "$S3"

step "2/4 minting a real bearer through Dex"
# The estate is governed, so an unauthenticated suite proves nothing. The password grant is the same
# one `e2e_stack.sh` uses; the static client and users come from the deployed configmap, so this
# follows the estate rather than assuming it.
DEX_CLIENT="$(kubectl get cm "$RELEASE-dex" -o jsonpath='{.data.config\.yaml}' 2>/dev/null | sed -n 's/^ *- id: *//p' | head -1)"
DEX_SECRET="$(kubectl get cm "$RELEASE-dex" -o jsonpath='{.data.config\.yaml}' 2>/dev/null | sed -n 's/^ *secret: *//p' | head -1)"
: "${DEX_CLIENT:=rask-web}" ; : "${DEX_SECRET:=lance-catalog-secret}"
# The audience the doors check, read off the catalog's own pod: a token without it is refused ([[LH-515]]), and Dex puts
# it in a token only when a client its audience client trusts asks for it with the cross-client scope.
OIDC_AUDIENCE="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="RASK_OIDC_AUDIENCE")].value}' 2>/dev/null)"
: "${OIDC_AUDIENCE:=rask-lakehouse}"
mint() {
  curl -s -m 20 "http://$DEX/dex/token" -d grant_type=password -d client_id="$DEX_CLIENT" \
    -d client_secret="$DEX_SECRET" -d scope="openid email federated:id audience:server:client_id:$OIDC_AUDIENCE" -d username="$1" -d password=password \
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
# The store's root pair, from the Secret the fixture's server mounts (`<release>-object-store-root`, written by ESO).
S3_KEY="$(kubectl get secret "$RELEASE-object-store-root" -o jsonpath='{.data.object-store-access-key}' 2>/dev/null | base64 -d || true)"
S3_SECRET="$(kubectl get secret "$RELEASE-object-store-root" -o jsonpath='{.data.object-store-secret-key}' 2>/dev/null | base64 -d || true)"

# AN ADDRESS THAT DID NOT RESOLVE IS EXPORTED AS NOTHING, never as the scheme alone. Every line below
# used to be `export X="http://$Y"` unconditionally, so a service this script could not find became the
# literal string `http://` — which no suite recognises as absent, so its own "needs X" skip never fires
# and the leg dies deep inside a client library instead. Measured 2026-09-14: one unfound object store
# produced 17 errors reading `ValueError: Invalid endpoint: http://`, none of which named the service
# or this script. Unset is the honest value: the suites already skip on it, and the roll-call below
# makes the absence visible rather than letting a skip pass for a pass.
_missing=""
url() {  # url VARNAME HOST[/path] — export VARNAME=http://HOST... only if HOST resolved
  local var="$1" host="$2" suffix="${3-}"
  if [ -n "$host" ]; then export "$var=http://$host$suffix"; else _missing="$_missing $var"; fi
}
url LANCE_E2E_CATALOG_URL     "$CATALOG"
url LANCE_E2E_LINEAGE_URL     "$LINEAGE"
url RASK_E2E_EDGE_URL         "$EDGE"
url LANCE_E2E_S3              "$S3"
url LANCE_E2E_S3_ENDPOINT     "$S3"
url LANCE_E2E_DEX             "$DEX" /dex
url LANCE_E2E_FGA             "$FGA"
url LANCE_E2E_GREPTIME_URL    "$GREPTIME"
url LANCE_E2E_MAINTENANCE_URL "$MAINT"
url LANCE_E2E_NOTIFICATIONS_URL "$NOTIFICATIONS" "$API_PREFIX"
url LANCE_E2E_ANNOTATOR_URL   "$ANNOTATOR"
if [ -n "$NATS" ]; then export LANCE_E2E_NATS_URL="nats://$NATS"; else _missing="$_missing LANCE_E2E_NATS_URL"; fi
[ -z "$_missing" ] || printf '   NOT DISCOVERED (their suites will skip):%s\n' "$_missing"
export LANCE_E2E_TOKEN="$ALICE"
export LANCE_E2E_ADMIN_TOKEN="$ALICE"
# NOT exported blind. A leg asserting a 403 with this token needs a genuine non-admin, and on THIS estate
# bob is a member of `team:eng` which is bound to `project:acme` — and `project.admin` is
# "[user, role#assignee] or member from team", so bob is an admin and the create succeeds. The suite
# then fails on a false premise and reads as a governance hole. It is not one; the identity was wrong.
# So the token is offered only when the estate agrees it is non-admin, and the suite SKIPS otherwise —
# an honest skip beats a red test that alleges something untrue.
if [ -n "$FGA" ]; then
  # OPENFGA ADMITS ONLY A PROJECTED `rask-openfga` TOKEN ([[XC-077]]), so this runner and the suites speak
  # to it as the bootstrap Job's ServiceAccount, the identity that writes the estate's standing tuples. A
  # TokenRequest, written to a file the suites re-read (`LANCE_E2E_FGA_TOKEN_FILE`); no secret is stored.
  FGA_TOKEN_FILE="$(mktemp)"
  trap 'rm -f "$FGA_TOKEN_FILE"' EXIT
  kubectl create token "$RELEASE-sa-jobs" --audience rask-openfga --duration 2h > "$FGA_TOKEN_FILE" \
    || fail "could not mint a rask-openfga token for $RELEASE-sa-jobs"
  export LANCE_E2E_FGA_TOKEN_FILE="$FGA_TOKEN_FILE"
  FGA_AUTH="Authorization: Bearer $(cat "$FGA_TOKEN_FILE")"
  # THE STORE THE ESTATE USES: the catalog's `RASK_FGA_STORE_ID` pin, else the newest store named
  # `lance-catalog` (`fga.newest_store`), the rule the hook and every service share ([[LH-201]]).
  # EXPORTED so a suite reads the SAME store this runner picked: a second pick by another rule agrees
  # only while the estate holds one store.
  STORE_ID="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[?(@.name=="catalog")].env[?(@.name=="RASK_FGA_STORE_ID")].value}' 2>/dev/null || true)"
  [ -n "$STORE_ID" ] || STORE_ID="$(curl -s -m 10 -H "$FGA_AUTH" "http://$FGA/stores" | uv run python -c "
import json, sys
from service_kit.governed.auth.write_model import STORE_NAME
from service_kit.governed.fga import newest_store
named = newest_store(json.load(sys.stdin).get('stores') or [], STORE_NAME)
print(named['id'] if named else '')" 2>/dev/null || true)"
  [ -n "$STORE_ID" ] && export LANCE_E2E_FGA_STORE_ID="$STORE_ID"
  # IN THAT STORE, THE MODEL THIS CHECKOUT CARRIES (`write_model.carried_model`). A check naming no model is
  # answered by the store's NEWEST, which a legacy image's narrower body can be. Absent, the probe below
  # does not run and the non-admin legs skip; the deployed-model check names the drift.
  MODEL_ID=""
  [ -z "$STORE_ID" ] || MODEL_ID="$(uv run python -c "
import sys
from service_kit.governed.auth.write_model import carried_model
try:
    print(carried_model(sys.argv[1], token_file=sys.argv[3], pinned=sys.argv[2])[1])
except LookupError as exc:
    sys.exit(f'   note: {exc}')" "http://$FGA" "$STORE_ID" "$FGA_TOKEN_FILE" || true)"
  # The principal the catalog keys a bearer on (LH-063), and the OpenFGA user it is, through the one seam.
  principal_of() { curl -s -m 20 "http://$CATALOG/management/v1/me" -H "authorization: Bearer $1" \
    | uv run python -c "import sys,json;print(json.load(sys.stdin).get('sub',''))" 2>/dev/null || true; }
  fga_user() { uv run python -c "import sys; from service_kit.governed.fga import user_object; print(user_object(sys.argv[1]))" "$1" 2>/dev/null || true; }
  can_administer() {
    curl -s -m 10 -X POST "http://$FGA/stores/$STORE_ID/check" -H "$FGA_AUTH" -H 'content-type: application/json' \
      -d "{\"authorization_model_id\":\"$MODEL_ID\",\"tuple_key\":{\"user\":\"$(fga_user "$1")\",\"relation\":\"can_administer\",\"object\":\"project:${LANCE_E2E_PROJECT:-acme}\"}}" \
      | uv run python -c "import sys,json;print(json.load(sys.stdin).get('allowed'))" 2>/dev/null || true
  }
  # ASK THE ESTATE WHICH IDENTITY IS ACTUALLY NON-ADMIN rather than naming one. The suite asserts a
  # 403, so offering a token that turns out to hold `can_administer` makes it fail on a false premise
  # and read as a governance hole — which is exactly what happened with bob. Candidates in the
  # deployed Dex configmap are tried in order and the first genuine non-admin wins.
  if [ -n "$MODEL_ID" ]; then
    for CAND_NAME in publisher bob; do
      eval "CAND_TOK=\${$(echo "$CAND_NAME" | tr '[:lower:]' '[:upper:]'):-}"
      [ -n "$CAND_TOK" ] || continue
      CAND_PRINCIPAL="$(principal_of "$CAND_TOK")"
      [ -n "$CAND_PRINCIPAL" ] || continue
      if [ "$(can_administer "$CAND_PRINCIPAL")" = "False" ]; then
        export LANCE_E2E_NONADMIN_TOKEN="$CAND_TOK"
        printf '   non-admin identity: %s (can_administer on %s is False)\n' "$CAND_NAME" "${LANCE_E2E_PROJECT:-acme}"
        break
      fi
    done
    [ -n "${LANCE_E2E_NONADMIN_TOKEN:-}" ] || printf '   note: every candidate holds can_administer on %s — the non-admin legs SKIP rather than fail falsely\n' "${LANCE_E2E_PROJECT:-acme}"
  fi
fi
# THE SERVICE IDENTITY criterion 2's matrix calls as (tests/e2e-py/test_the_catalog_is_correct.py): the lineage service's
# account, for the audience the catalog's own door checks, read off the deployed catalog container. An account
# authenticates whatever namespace it runs in, and what it may do is the tuples that name it.
SA_AUDIENCE="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[?(@.name=="catalog")].env[?(@.name=="RASK_SA_AUDIENCE")].value}' 2>/dev/null || true)"
if [ -n "$SA_AUDIENCE" ]; then
  SERVICE_TOKEN_FILE="$(mktemp)"
  trap 'rm -f "${FGA_TOKEN_FILE:-}" "$SERVICE_TOKEN_FILE"' EXIT
  kubectl create token "$RELEASE-sa-lineage" --audience "$SA_AUDIENCE" --duration 2h > "$SERVICE_TOKEN_FILE" \
    || fail "could not mint a $SA_AUDIENCE token for $RELEASE-sa-lineage"
  export LANCE_E2E_SERVICE_TOKEN_FILE="$SERVICE_TOKEN_FILE"
fi
export LANCE_E2E_DAPR_TOKEN="$DAPR_TOKEN"
export LANCE_E2E_OIDC_CLIENT_ID="$DEX_CLIENT"
export LANCE_E2E_OIDC_AUDIENCE="$OIDC_AUDIENCE"
export LANCE_E2E_OIDC_CLIENT_SECRET="$DEX_SECRET"
export LANCE_E2E_DEX_SECRET="$DEX_SECRET"
export LANCE_E2E_PROJECT="${LANCE_E2E_PROJECT:-acme}"
export LANCE_E2E_RELEASE="$RELEASE"
# The namespace the release runs in: its ServiceAccounts are the services' signing identities.
LANCE_E2E_RELEASE_NAMESPACE="$(kubectl config view --minify -o jsonpath='{..namespace}' 2>/dev/null || true)"
export LANCE_E2E_RELEASE_NAMESPACE="${LANCE_E2E_RELEASE_NAMESPACE:-default}"
# The namespace lineage keeps its feed and edge list in (`services.lineage.provenanceNamespace`), read off the deployed
# lineage container: criterion 1's module reads the edge list there.
PROVENANCE_NS="$(kubectl get deploy "$RELEASE-lineage" -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="LINEAGE_PROVENANCE_NAMESPACE")].value}' 2>/dev/null || true)"
[ -z "$PROVENANCE_NS" ] || export LANCE_E2E_PROVENANCE_NAMESPACE="$PROVENANCE_NS"
export LANCE_E2E_OUTBOX_URI="s3://lance-catalog/_lineage_outbox"
# The control lane's outbox, read off the deployed catalog container: criterion 5's drills hold it drained.
CONTROL_OUTBOX="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[?(@.name=="catalog")].env[?(@.name=="LANCE_CONTROL_OUTBOX_URI")].value}' 2>/dev/null || true)"
[ -z "$CONTROL_OUTBOX" ] || export LANCE_E2E_CONTROL_OUTBOX_URI="$CONTROL_OUTBOX"
export LANCE_E2E_RELAY_BINDING=lineage-relay-cron
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
export LANCE_E2E_AUTH_SERVER="http://$CATALOG"
export MEDIA_CATALOG_URL="http://$CATALOG"
export LANCE_REST_E2E_URL="http://$CATALOG"
# THE CREDENTIAL TRAVELS WITH THE ADDRESS. Bridging `MEDIA_CATALOG_URL` alone pointed that suite at a
# GOVERNED catalog anonymously — an empty token is its documented "auth-off" mode, so it took the
# ungoverned branch and every leg 401'd on "Missing bearer token". The catalog was right; the runner
# handed it half an identity.
export MEDIA_CATALOG_TOKEN="$ALICE"

# THE WAREHOUSE IS DISCOVERED, not assumed, because `warehouses.enabled` decides which door a suite
# must use. With warehouses on, a top-level `POST /v1/namespace/{ns}/create` is refused 400 by
# `require_warehouse_scoped` — deliberately — and the suites that read `LANCE_E2E_WAREHOUSE` switch to
# the warehouse-scoped door only when it is set. Unset, they took the root door and read a correct
# refusal as an authorization defect. Left empty when the catalog serves no warehouses, which is what
# an estate with the feature off looks like.
# `GET /management/v1/warehouses` answers a BARE LIST (92 records on this estate), not an envelope — measured
# 2026-09-06. Handle the envelope shapes too rather than pinning the bare one: this is discovery, and
# a runner that dies on a response shape is the failure it exists to prevent.
WAREHOUSE="$(curl -s -m 15 "http://$CATALOG/management/v1/warehouses" -H "authorization: Bearer $ALICE" | uv run python -c "
import json, os, sys
try:
    body = json.load(sys.stdin)
except Exception:
    sys.exit()
if isinstance(body, list):
    records = body
elif isinstance(body, dict):
    records = body.get('warehouses') or body.get('items') or []
else:
    records = []
project = os.environ.get('LANCE_E2E_PROJECT', 'acme')
# ACTIVE only, and the oldest first: a suite that adopts an existing namespace wants the estate's
# settled warehouse, not one a previous e2e run minted and may yet remove.
matches = sorted(
    (r for r in records if isinstance(r, dict) and r.get('project') == project and r.get('status', 'active') == 'active'),
    key=lambda r: str(r.get('created_at', '')),
)
if matches:
    print(matches[0].get('id') or matches[0].get('name') or '')
" 2>/dev/null || true)"
if [ -n "$WAREHOUSE" ]; then
  export LANCE_E2E_WAREHOUSE="$WAREHOUSE"
  printf '   warehouse: %s (project %s)\n' "$WAREHOUSE" "${LANCE_E2E_PROJECT:-acme}"
else
  printf '   note: no warehouse found for project %s — the suites that need one use the root door and may be refused\n' "${LANCE_E2E_PROJECT:-acme}"
fi

# ---- a SECOND tenant, so the credential-isolation legs can run ----------------------------------
# Five legs in `test_credential_isolation_e2e.py` assert that a credential vended for tenant A is
# REFUSED on tenant B's data — the sharpest statement of storage isolation this estate makes — and all
# five skipped, because the runner that discovers every other address and credential from the cluster
# set neither variable they gate on.
#
# A FIXED ID, deliberately, not one per run. This is estate FURNITURE like `acme`, created once and
# reused: a per-run tenant would leave a project behind on every drive, and a project has no retention
# to clean it up (§ Q8-15 is what accumulation does to a control). `POST /management/v1/projects` writes the
# record and the creator's admin tuple in one operation. Measured against the deployed catalog: a
# SECOND create answers 200 and echoes the current admin set rather than 409 — it converges. 409 is
# accepted below anyway, because which of the two a door gives for "already there" is exactly the kind
# of thing that changes under you, and both mean the same to this script.
#
# GUARDED like the warehouse block above: an estate that refuses the create prints a note and the legs
# skip exactly as they do today. Provisioning a tenant must never be the reason a suite run dies.
if [ -n "$CATALOG" ] && [ -n "$ALICE" ] && [ -n "$BOB" ]; then
  TENANT_B="${LANCE_E2E_PROJECT_B:-e2etenantb}"
  B_CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST "http://$CATALOG/management/v1/projects" \
    -H "authorization: Bearer $ALICE" -H 'content-type: application/json' \
    -d "{\"id\":\"$TENANT_B\"}" 2>/dev/null || true)"
  BOB_PRINCIPAL="$(curl -s -m 20 "http://$CATALOG/management/v1/me" -H "authorization: Bearer $BOB" \
    | uv run python -c "import sys,json;print(json.load(sys.stdin).get('sub',''))" 2>/dev/null || true)"
  if [ "$B_CODE" = "200" ] || [ "$B_CODE" = "201" ] || [ "$B_CODE" = "409" ]; then
    # Bob administers tenant B while alice administers acme, which is what makes the isolation legs a
    # real cross-tenant test rather than one principal talking to two of its own projects.
    [ -n "$BOB_PRINCIPAL" ] && curl -s -o /dev/null -m 20 -X POST "http://$CATALOG/management/v1/access/tuples" \
      -H "authorization: Bearer $ALICE" -H 'content-type: application/json' \
      -d "{\"user\":\"$BOB_PRINCIPAL\",\"relation\":\"admin\",\"object\":\"project:$TENANT_B\"}" 2>/dev/null || true
    export LANCE_E2E_PROJECT_B="$TENANT_B"
    export LANCE_E2E_TENANT_B_TOKEN="$BOB"
    printf '   tenant B: %s (admin=bob) — the credential-isolation legs will RUN\n' "$TENANT_B"
  else
    printf '   note: could not provision tenant B (HTTP %s) — the 5 credential-isolation legs will SKIP\n' "${B_CODE:-?}"
  fi
fi

# ---- settle gate -------------------------------------------------------------------------------
# A SETTLING ESTATE AND A BROKEN ONE LOOKED IDENTICAL, and that is the whole reason this exists. The
# suites assert on what bus subscribers did — and after a rollout a subscriber's sidecar takes time to
# re-register its subscriptions. A suite that starts inside that window publishes to a topic nobody is
# listening on yet and reports a failure, so a green run needed a second drive and the first one taught
# nothing.
#
# THE PROBE IS THE SIDECAR'S OWN ANSWER, not a sleep: `/v1.0/metadata` reports the subscriptions the
# runtime has actually registered plus `actorRuntime.hostReady` (notifications hosts the inbox actors). A
# fixed sleep would be both slower on a settled estate and still wrong on a slow one.
#
# It FAILS rather than warns: proceeding into a suite that cannot pass produces a red run whose cause
# is the clock, and this script's whole job is to make a red run mean something.
settle_deadline=$(( $(date +%s) + ${LANCE_E2E_SETTLE_SECONDS:-120} ))
step "3.5/4 waiting for the subscribers' subscriptions to re-register"
for dep in notifications; do
  # ROLLOUT FIRST, THEN THE SIDECAR — and that order is the fix for a hole this gate had when it was
  # first driven. Mid-rollout `kubectl exec deploy/<name>` may land on the OLD pod, which is still
  # serving and still settled, so the probe answered "ready" 1 second after a restart while the NEW
  # pod had not registered anything. Waiting for the deployment to converge first means the pod the
  # probe reaches is the pod the suite will actually be talking to.
  kubectl rollout status "deploy/$RELEASE-$dep" --timeout="${LANCE_E2E_SETTLE_SECONDS:-120}s" >/dev/null 2>&1 \
    || fail "$dep did not finish rolling out within ${LANCE_E2E_SETTLE_SECONDS:-120}s"
  while :; do
    if kubectl exec "deploy/$RELEASE-$dep" -- python -c '
import json, sys, urllib.request
m = json.load(urllib.request.urlopen("http://127.0.0.1:3500/v1.0/metadata", timeout=5))
topics = {s.get("topic") for s in (m.get("subscriptions") or [])}
ready = (m.get("actorRuntime") or {}).get("hostReady")
sys.exit(0 if topics and ready else 1)
' >/dev/null 2>&1; then
      printf '   ok   %s
' "$dep"; break
    fi
    [ "$(date +%s)" -lt "$settle_deadline" ] || fail "$dep has not re-registered its subscriptions within ${LANCE_E2E_SETTLE_SECONDS:-120}s — the estate is not settled, and running now would report the clock as a defect"
    sleep 3
  done
done

step "4/4 running the suites against the live estate"
# `-m e2e` and NOT a file list: the marker is what the suites declare about themselves, so a new suite
# is picked up without editing this script. Args override for a narrower run.
#
# A RUN THAT NAMES NO PATH IS ROOTED AT tests/e2e-py. pytest reads its arguments before it loads the conftest that
# defines `--keep-drive PATH` and `--soak PATH`, so with no path it takes that PATH for the run's own path and never
# loads the conftest: measured on pytest 9.1.1, `-m phase1 --keep-drive PATH` stopped at "unrecognized arguments".
ROOT=(tests/e2e-py)
for arg in "$@"; do
  case "$arg" in tests/* | */tests/*) ROOT=() ;; esac
done
[ "$#" -gt 0 ] || set -- -m e2e
PYTHONPATH=services uv run pytest "${ROOT[@]}" "$@" -v
