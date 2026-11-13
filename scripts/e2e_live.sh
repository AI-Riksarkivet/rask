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

. "$(dirname "${BASH_SOURCE[0]}")/e2e_credentials.sh"
trap 'rm -rf "$CREDENTIALS"' EXIT

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
mint() { password_grant "http://$DEX/dex/token" "$DEX_CLIENT" "$DEX_SECRET" "openid email federated:id audience:server:client_id:$OIDC_AUDIENCE" "$1" password; }
ALICE="$(mint alice@example.com)"
# A THIRD IDENTITY, because bob is not the non-admin this estate needs. The seed assigns bob to acme's
# admins role, which holds `admin` on `project:acme` — so bob holds `can_administer` and the 403 legs had
# nothing to assert. `publisher@rask.internal` is in the deployed Dex configmap and is in no role; measured
# 2026-09-06: can_administer(project:acme) is False for publisher and True for bob.
PUBLISHER="$(mint publisher@rask.internal)"
[ -n "$ALICE" ] || fail "Dex issued no token for alice — is auth enabled and dex reachable at $DEX?"
printf '   alice=%s…\n' "${ALICE:0:18}"

step "3/4 reading the estate's own secrets"
# From the Dapr app-token Secret, never from a values file: the token the sidecar stamps is the one
# the doors verify, and any other copy is a guess.
keep dapr-token "$(kubectl get secret "$RELEASE-dapr-app-token" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
# THE SUITES' OWN STORE IDENTITY ([[XC-081]]): a token the suites exchange at the store's STS, never the store's root pair.
. "$(dirname "$0")/suites_store_token.sh"
minted="$(suites_store_token "$RELEASE")" || fail "could not mint a store token for rask-e2e-suites (is deploy/values-e2e-suites.yaml in the release?)"
keep store-token "$minted"
keep token "$ALICE"
keep dex-secret "$DEX_SECRET"

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
if [ -n "$NATS" ]; then export LANCE_E2E_NATS_URL="nats://$NATS"; else _missing="$_missing LANCE_E2E_NATS_URL"; fi
[ -z "$_missing" ] || printf '   NOT DISCOVERED (their suites will skip, which fails the run):%s\n' "$_missing"
if [ -n "$FGA" ]; then
  # OPENFGA ADMITS ONLY A PROJECTED `rask-openfga` TOKEN ([[XC-077]]), so this runner and the suites speak
  # to it as the bootstrap Job's ServiceAccount, the identity that writes the estate's standing tuples. A
  # TokenRequest, written to the file the suites re-read (`fga-token`); no secret is stored.
  minted="$(kubectl create token "$RELEASE-sa-jobs" --audience rask-openfga --duration 2h)" \
    || fail "could not mint a rask-openfga token for $RELEASE-sa-jobs"
  keep fga-token "$minted"
  # THE STORE THE ESTATE USES: the catalog's `RASK_FGA_STORE_ID` pin, else the newest store named
  # `lance-catalog` (`fga.newest_store`), the rule the hook and every service share ([[LH-201]]).
  # EXPORTED so a suite reads the SAME store this runner picked: a second pick by another rule agrees
  # only while the estate holds one store.
  STORE_ID="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[?(@.name=="catalog")].env[?(@.name=="RASK_FGA_STORE_ID")].value}' 2>/dev/null || true)"
  [ -n "$STORE_ID" ] || STORE_ID="$(curl -s -m 10 -H @- "http://$FGA/stores" <<<"Authorization: Bearer $(cat "$CREDENTIALS/fga-token")" | uv run python -c "
import json, sys
from lakehouse.governed.auth.write_model import STORE_NAME
from lakehouse.governed.fga import newest_store
named = newest_store(json.load(sys.stdin).get('stores') or [], STORE_NAME)
print(named['id'] if named else '')" 2>/dev/null || true)"
  [ -n "$STORE_ID" ] && export LANCE_E2E_FGA_STORE_ID="$STORE_ID"
fi
# THE SERVICE IDENTITY criterion 2's matrix calls as (tests/e2e-py/test_the_catalog_is_correct.py): the lineage service's
# account, for the audience the catalog's own door checks, read off the deployed catalog container. An account
# authenticates whatever namespace it runs in, and what it may do is the tuples that name it.
SA_AUDIENCE="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[?(@.name=="catalog")].env[?(@.name=="RASK_SA_AUDIENCE")].value}' 2>/dev/null || true)"
if [ -n "$SA_AUDIENCE" ]; then
  minted="$(kubectl create token "$RELEASE-sa-lineage" --audience "$SA_AUDIENCE" --duration 2h)" \
    || fail "could not mint a $SA_AUDIENCE token for $RELEASE-sa-lineage"
  keep service-token "$minted"
fi
export LANCE_E2E_CREDENTIALS="$CREDENTIALS"
export LANCE_E2E_OIDC_CLIENT_ID="$DEX_CLIENT"
export LANCE_E2E_OIDC_AUDIENCE="$OIDC_AUDIENCE"
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
# The store's pair rides as the files above, never as AWS_*: a pair in the environment is every client's ambient
# credential, so a vended session missing a key would silently sign as the root.
export LANCE_E2E_S3_BUCKET="${LANCE_E2E_S3_BUCKET:-lance-catalog}"
export LANCE_E2E_S3_REGION="${LANCE_E2E_S3_REGION:-us-east-1}"

# ONE ADDRESS, SEVERAL NAMES. These are not aliases kept for compatibility — each suite was written
# against a different bring-up (a port-forward, `e2e_stack.sh`, a local compose) and named the same
# service for what that bring-up called it. The runner's job is to answer to all of them rather than
# to make thirty suites agree on a spelling.
export LANCE_E2E_AUTH_SERVER="http://$CATALOG"
export LANCE_REST_E2E_URL="http://$CATALOG"

# THE WAREHOUSE IS DISCOVERED, not assumed. Every name's first segment is its warehouse, so a suite creates its
# namespaces as `<LANCE_E2E_WAREHOUSE>$<namespace>` through the spec's own door: the project's oldest active warehouse,
# so a suite's grants are the project's, or else the estate's own warehouse, the first segment of the release's
# provenance namespace, which belongs to no project.
# `GET /management/v1/warehouses` answers a BARE LIST (92 records on this estate), not an envelope — measured
# 2026-09-06. Handle the envelope shapes too rather than pinning the bare one: this is discovery, and
# a runner that dies on a response shape is the failure it exists to prevent.
WAREHOUSE="$(authorized "$ALICE" -s -m 15 "http://$CATALOG/management/v1/warehouses" | uv run python -c "
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
# ACTIVE only, and the oldest first: a suite wants the estate's settled warehouse, not one a previous e2e run minted
# and may yet remove.
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
elif [ -n "$PROVENANCE_NS" ]; then
  export LANCE_E2E_WAREHOUSE="${PROVENANCE_NS%%\$*}"
  printf '   note: no warehouse found for project %s — the suites create in the estate warehouse %s, which no project holds\n' "${LANCE_E2E_PROJECT:-acme}" "$LANCE_E2E_WAREHOUSE"
else
  printf '   note: no warehouse found for project %s and no provenance namespace on %s-lineage — LANCE_E2E_WAREHOUSE is unset\n' "${LANCE_E2E_PROJECT:-acme}" "$RELEASE"
fi

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
# THE MULTIBASE FAN-OUT LEG needs two data bases the deployed catalog admits (`catalog.multibase.dataBases`), read off its
# own container. An estate declaring fewer cannot serve it, so the default selection leaves that one leg out and says so;
# a run that names it gets what it named, and fails under `--require-live` when it skips.
BASES="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[?(@.name=="catalog")].env[?(@.name=="LANCE_MULTIBASE_DATA_BASES")].value}' 2>/dev/null || true)"
IFS=, read -r -a DATA_BASES <<<"$BASES"
NOT_SERVED=()
if [ "${#DATA_BASES[@]}" -ge 2 ]; then
  export LANCE_E2E_BASE_A="${DATA_BASES[0]}" LANCE_E2E_BASE_B="${DATA_BASES[1]}"
else
  NOT_SERVED=(--deselect tests/e2e-py/test_multibase_e2e.py::test_multibase_redirects_data_and_reads_fan_out)
  printf '   the catalog admits %s data base(s), so the multibase fan-out leg is left out of the default selection\n' "${#DATA_BASES[@]}"
fi

# A SKIP IS A FAILED DRIVE (`--require-live`): the estate is up, so a suite that skipped never ran. The default selection
# leaves out the two markers whose rig only their own Dagger lane stands up, as `scripts/e2e_stack.sh` does: `nats_auth`
# (an operator-mode broker, `dagger call nats-auth`) and `rustfs_provisioning` (a throwaway store, `dagger call
# rustfs-provisioning`).
[ "$#" -gt 0 ] || set -- -m "e2e and not nats_auth and not rustfs_provisioning" "${NOT_SERVED[@]}"
PYTHONPATH=services uv run pytest "${ROOT[@]}" "$@" -v --require-live
