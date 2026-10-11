#!/usr/bin/env bash
# Install development fixtures (deploy/fixtures/<name>), each as its own Helm release, before the rask chart.
#
#   scripts/fixtures.sh <name>... [-- <extra helm args for every fixture named>]
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

NAMES=()
EXTRA=()
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--" ]; then shift; EXTRA=("$@"); break; fi
  NAMES+=("$1"); shift
done
set -- "${NAMES[@]}"
[ "$#" -gt 0 ] || { echo "usage: $0 <fixture>... (one of: $(ls "$ROOT/deploy/fixtures" | grep -v README | tr '\n' ' '))" >&2; exit 2; }

for name in "$@"; do
  dir="$ROOT/deploy/fixtures/$name"
  [ -f "$dir/fixture.env" ] || { echo "no fixture '$name' (deploy/fixtures/$name/fixture.env)" >&2; exit 2; }
  RELEASE='' NAMESPACE='' REPO='' CHART='' VERSION='' SHARED_NAMESPACE=''
  # shellcheck source=/dev/null
  . "$dir/fixture.env"
  for key in RELEASE NAMESPACE CHART; do
    [ -n "${!key}" ] || { echo "deploy/fixtures/$name/fixture.env sets no $key" >&2; exit 2; }
  done
  # An upstream chart names REPO and VERSION; a fixture's own chart is a directory beside its fixture.env.
  if [ -n "$REPO" ]; then
    [ -n "$VERSION" ] || { echo "deploy/fixtures/$name/fixture.env names REPO without VERSION" >&2; exit 2; }
    SOURCE=("$CHART" --repo "$REPO" --version "$VERSION")
  else
    SOURCE=("$dir/$CHART")
  fi
  # values.yaml is the fixture's settings; values-live-pins.yaml, when present, is what the cluster runs (an image tag
  # from a dev registry), written by scripts/k3s-pins.sh and layered last so a rerun never reverts a running image.
  VALUES=()
  [ -f "$dir/values.yaml" ] && VALUES+=(-f "$dir/values.yaml")
  [ -f "$dir/values-live-pins.yaml" ] && VALUES+=(-f "$dir/values-live-pins.yaml")

  # THE HANDOVER. Every object this fixture renders that already exists under another Helm release is annotated
  # helm.sh/resource-policy=keep first, so that release's next upgrade (the one that stops rendering it) leaves it
  # standing, and the install takes ownership of it. An object no release owns is adopted the same way. Nothing is
  # deleted and recreated: a RayCluster or a CRD changes owner in place. A kind the cluster does not serve yet (a CRD
  # the fixture itself installs) has nothing to hand over, so its lookup error is not a failure.
  OWN=()
  held="$("$HELM" template "$RELEASE" "${SOURCE[@]}" --namespace "$NAMESPACE" --include-crds "${VALUES[@]}" \
    | { kubectl get --namespace "$NAMESPACE" --ignore-not-found -o json -f - 2>/dev/null || true; } \
    | python3 -c '
import json, sys
release = sys.argv[1]
raw = sys.stdin.read().strip()
found = json.loads(raw) if raw.startswith("{") else {}
# One resource comes back as itself, several as a List.
items = found.get("items", []) if found.get("kind") == "List" else [found] if found else []
for item in items:
    meta = item["metadata"]
    owner = (meta.get("annotations") or {}).get("meta.helm.sh/release-name")
    if owner != release:
        # "-" for an absent field: `read` collapses runs of blanks, so an empty namespace would shift the owner left.
        print(item["kind"], meta["name"], meta.get("namespace") or "-", owner or "-")
' "$RELEASE")"
  if [ -n "$held" ]; then
    while read -r kind obj ns owner; do
      NS=()
      [ "$ns" != "-" ] && NS=(--namespace "$ns")
      kubectl annotate "$kind" "$obj" "${NS[@]}" helm.sh/resource-policy=keep --overwrite >/dev/null
      echo ">> $name: $kind/$obj (release $owner) kept and handed to release $RELEASE"
    done <<<"$held"
    OWN=(--take-ownership)
  fi

  echo ">> fixture $name: ${SOURCE[*]} as release $RELEASE in $NAMESPACE"
  "$HELM" upgrade --install "$RELEASE" "${SOURCE[@]}" \
    --namespace "$NAMESPACE" --create-namespace --reset-then-reuse-values "${VALUES[@]}" "${OWN[@]}" "${EXTRA[@]}" \
    --description "rask fixture: not for production" --wait --timeout 600s
  # A fixture in a namespace it shares with rask (SHARED_NAMESPACE=true) leaves the namespace unlabelled: the mark says
  # everything in it is throwaway, which rask's own namespace is not.
  if [ "$SHARED_NAMESPACE" != "true" ]; then
    kubectl label namespace "$NAMESPACE" "$MARK" --overwrite >/dev/null
  fi
done
