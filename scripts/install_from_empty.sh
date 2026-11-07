#!/usr/bin/env bash
# Install rask the way a platform team does, onto the cluster KUBECONFIG names, which must be empty ([[XC-133]],
# ADR 0153): the development prerequisites and stand-ins from deploy/fixtures, each its own release, then the product
# chart, then wait until every workload is Ready. On failure it prints why: every pod's state, each failed container's
# exit reason, and the logs of whatever is not Ready, every Job pod's included.
#
# The images the render names (`<component>:$TAG`, image.localImages) must already be pullable by the cluster:
# `dagger call install-from-empty` builds them into a registry its k3s mirrors, `dagger call lakehouse` copies a release's
# published images there by digest, and scripts/e2e_stack.sh loads them into kind.
#
# CHART is the chart to install: this tree's ./chart by default, or a published release's OCI reference with
# CHART_VERSION naming its version ([[XC-187]]).
#
# A client's e2e against a pinned lakehouse ([[XC-189]]) passes the integration its own release sets: VALUES names a
# values file (`networkPolicy.objectStoreExtraFrom`; a key HELM_SET sets stays the stack's, as Helm applies `--set` over
# every values file), and
# DEX_CLIENT, DEX_CLIENT_SECRET and DEX_REDIRECT_URIS (a JSON list) register the client's own sign-in client with the Dex
# fixture (its `clients`), which the lakehouse's audience trusts. The client's roles are granted to its own identity at
# the catalog's access doors once the stack is up ([[LH-563]]), never in values.
#
# CONSOLE_ENABLED=false installs the lakehouse without its console (`console.enabled`): `dagger call lakehouse` sets it,
# because a published release's image index holds the catalog image alone, and a client's e2e drives the lakehouse's
# doors, not its UI. Every other caller installs the product whole, the console included.
set -euo pipefail
cd "$(dirname "$0")/.."

RELEASE="${RELEASE:-rask}"
TAG="${TAG:-dev}"
CHART="${CHART:-./chart}"
CHART_VERSION="${CHART_VERSION:-}"
VALUES="${VALUES:-}"
CONSOLE_ENABLED="${CONSOLE_ENABLED:-true}"
DEX_VALUES=()
if [ -n "${DEX_CLIENT:-}" ]; then
  # JSON is YAML, and json.dumps quotes whatever the secret holds; the file is the install's alone.
  dex_values="$(mktemp)"
  python3 -c 'import json, os; print(json.dumps({"clients": [{"id": os.environ["DEX_CLIENT"], "secret": os.environ["DEX_CLIENT_SECRET"], "redirectURIs": json.loads(os.environ.get("DEX_REDIRECT_URIS") or "[]")}]}))' >"$dex_values"
  DEX_VALUES=(-- -f "$dex_values")
fi
TIMEOUT="${TIMEOUT:-900s}"
# shellcheck source=scripts/stack_values.sh
. scripts/stack_values.sh
# shellcheck source=scripts/install_common.sh
. scripts/install_common.sh

JOB_LOGS="$(mktemp -d)"
RECORDER=""
diagnose() {
  local rc=$?
  # The client's secret leaves with the install.
  [ -n "${dex_values:-}" ] && rm -f "$dex_values"
  [ -n "$RECORDER" ] && kill "$RECORDER" 2>/dev/null
  [ "$rc" -eq 0 ] && return 0
  echo "==> install-from-empty FAILED (exit $rc)"
  print_cluster_state "$JOB_LOGS"
  return "$rc"
}
trap diagnose EXIT

echo "==> fixtures"
install_stack_fixtures "${DEX_VALUES[@]}"

echo "==> rask"
start_job_recorder "$JOB_LOGS"
scripts/helm.sh upgrade --install "$RELEASE" "$CHART" ${CHART_VERSION:+--version "$CHART_VERSION"} --timeout "$TIMEOUT" "${HELM_SET[@]}" \
  --set "console.enabled=$CONSOLE_ENABLED" ${VALUES:+-f "$VALUES"}

echo "==> ready"
kubectl wait --for=condition=Available deploy --all --timeout="$TIMEOUT"
for s in $(kubectl get statefulsets -o name); do kubectl rollout status "$s" --timeout="$TIMEOUT"; done
kubectl get pods -o wide
echo "==> rask installed from empty: $(kubectl get deploy,statefulset --no-headers | wc -l) workloads Ready"
