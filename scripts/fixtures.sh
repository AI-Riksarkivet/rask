#!/usr/bin/env bash
# Install development fixtures (deploy/fixtures/<name>), each as its own Helm release, before the rask chart.
#
#   scripts/fixtures.sh <name>...
#
# NOT FOR PRODUCTION: a fixture stands in for what a platform team runs (deploy/fixtures/README.md, ADR 0153).
# Every helm call goes through scripts/helm.sh, so RASK_EXPECT_CONTEXT and the kubeconfig guard hold here too.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELM="$ROOT/scripts/helm.sh"
MARK="rask.dev/fixture=not-for-production"

# The kubectl calls below pick the same cluster scripts/helm.sh picks: the k3s estate when nothing is named and no
# other context is expected, and otherwise only the context the caller declared (~/.kube/config can name a dead one).
K3S=/etc/rancher/k3s/k3s.yaml
if [ -z "${KUBECONFIG:-}" ] && [ -r "$K3S" ]; then
  if [ -z "${RASK_EXPECT_CONTEXT:-}" ] || [ "$(KUBECONFIG="$K3S" kubectl config current-context 2>/dev/null || true)" = "$RASK_EXPECT_CONTEXT" ]; then
    export KUBECONFIG="$K3S"
  fi
fi
if [ -n "${RASK_EXPECT_CONTEXT:-}" ] && [ "$(kubectl config current-context)" != "$RASK_EXPECT_CONTEXT" ]; then
  echo "kubectl's context is $(kubectl config current-context), not $RASK_EXPECT_CONTEXT" >&2; exit 2
fi

[ "$#" -gt 0 ] || { echo "usage: $0 <fixture>... (one of: $(ls "$ROOT/deploy/fixtures" | grep -v README | tr '\n' ' '))" >&2; exit 2; }

for name in "$@"; do
  dir="$ROOT/deploy/fixtures/$name"
  [ -f "$dir/fixture.env" ] || { echo "no fixture '$name' (deploy/fixtures/$name/fixture.env)" >&2; exit 2; }
  RELEASE='' NAMESPACE='' REPO='' CHART='' VERSION='' ADOPT_CRDS=''
  # shellcheck source=/dev/null
  . "$dir/fixture.env"
  for key in RELEASE NAMESPACE REPO CHART VERSION; do
    [ -n "${!key}" ] || { echo "deploy/fixtures/$name/fixture.env sets no $key" >&2; exit 2; }
  done

  # The handover: keep first, so no later upgrade of the CRD's current release can delete it, then adopt.
  OWN=()
  for crd in $ADOPT_CRDS; do
    if kubectl get crd "$crd" >/dev/null 2>&1; then
      kubectl annotate crd "$crd" helm.sh/resource-policy=keep --overwrite >/dev/null
      echo ">> $name: $crd kept and handed to release $RELEASE"
      OWN=(--take-ownership)
    fi
  done

  VALUES=()
  [ -f "$dir/values.yaml" ] && VALUES=(-f "$dir/values.yaml")
  echo ">> fixture $name: $CHART $VERSION as release $RELEASE in $NAMESPACE"
  "$HELM" upgrade --install "$RELEASE" "$CHART" --repo "$REPO" --version "$VERSION" \
    --namespace "$NAMESPACE" --create-namespace "${VALUES[@]}" "${OWN[@]}" \
    --description "rask fixture: not for production" --wait --timeout 600s
  kubectl label namespace "$NAMESPACE" "$MARK" --overwrite >/dev/null
done
