#!/usr/bin/env bash
# Install development fixtures (deploy/fixtures/<name>), each as its own Helm release, before the rask chart.
#
#   scripts/fixtures.sh <name>... [-- <extra helm args for every fixture named>]
#   scripts/fixtures.sh --vendor [<name>...]   vendor each local fixture chart's dependencies (all by default), no cluster
#
# NOT FOR PRODUCTION: a fixture stands in for what a platform team runs (deploy/fixtures/README.md, ADR 0153).
# Every helm call goes through scripts/helm.sh, so RASK_EXPECT_CONTEXT and the kubeconfig guard hold here too.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELM="$ROOT/scripts/helm.sh"
MARK="rask.dev/fixture=not-for-production"

VENDOR_ONLY=false
if [ "${1:-}" = "--vendor" ]; then
  VENDOR_ONLY=true; shift
  [ "$#" -gt 0 ] || set -- $(cd "$ROOT/deploy/fixtures" && ls -d -- */ | tr -d /)
fi

# The kubectl calls below pick the same cluster scripts/helm.sh picks: the k3s estate when nothing is named and no
# other context is expected, and otherwise only the context the caller declared (~/.kube/config can name a dead one).
K3S=/etc/rancher/k3s/k3s.yaml
if ! "$VENDOR_ONLY" && [ -z "${KUBECONFIG:-}" ] && [ -r "$K3S" ]; then
  if [ -z "${RASK_EXPECT_CONTEXT:-}" ] || [ "$(KUBECONFIG="$K3S" kubectl config current-context 2>/dev/null || true)" = "$RASK_EXPECT_CONTEXT" ]; then
    export KUBECONFIG="$K3S"
  fi
fi
if ! "$VENDOR_ONLY" && [ -n "${RASK_EXPECT_CONTEXT:-}" ] && [ "$(kubectl config current-context)" != "$RASK_EXPECT_CONTEXT" ]; then
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
  RELEASE='' NAMESPACE='' REPO='' CHART='' VERSION='' SHARED_NAMESPACE='' SET_FILES='' WAIT=''
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
    # A local chart that names upstream charts as dependencies vendors them first; `dependency build` resolves only
    # repositories the local helm config knows, so each one the chart declares is added under a name derived from it.
    if grep -q '^dependencies:' "$dir/$CHART/Chart.yaml"; then
      REPOS=()
      while read -r repo; do
        REPOS+=("fixture-$(printf '%s' "$repo" | sha256sum | cut -c1-8)")
        "$HELM" repo add "${REPOS[-1]}" "$repo" >/dev/null
      done < <("$HELM" dependency list "$dir/$CHART" | awk 'NR > 1 && $3 ~ /^https?:/ { print $3 }' | sort -u)
      "$HELM" repo update "${REPOS[@]}" >/dev/null
      "$HELM" dependency build "$dir/$CHART" >/dev/null
    fi
  fi
  "$VENDOR_ONLY" && continue
  # values.yaml is the fixture's settings; values-live-pins.yaml, when present, is what the cluster runs (an image tag
  # from a dev registry), written by scripts/k3s-pins.sh and layered last so a rerun never reverts a running image.
  VALUES=()
  [ -f "$dir/values.yaml" ] && VALUES+=(-f "$dir/values.yaml")
  [ -f "$dir/values-live-pins.yaml" ] && VALUES+=(-f "$dir/values-live-pins.yaml")
  # SET_FILES names repo files a value is read from (`<key>=<path from the repo root>`, space-separated): an artefact
  # that lives outside the fixture's chart, as the alerting rules do.
  for pair in $SET_FILES; do
    [ -f "$ROOT/${pair#*=}" ] || { echo "deploy/fixtures/$name/fixture.env: SET_FILES names ${pair#*=}, which does not exist" >&2; exit 2; }
    VALUES+=(--set-file "${pair%%=*}=$ROOT/${pair#*=}")
  done

  # THE HANDOVER. Every object this fixture renders that already exists under another Helm release is annotated
  # helm.sh/resource-policy=keep first, so that release's next upgrade (the one that stops rendering it) leaves it
  # standing, and the install takes ownership of it. An object no release owns is adopted the same way: a RayCluster
  # or a CRD changes owner in place. A kind the cluster does not serve yet (a CRD the fixture itself installs) has
  # nothing to hand over, so its lookup error is not a failure.
  #
  # A WORKLOAD WHOSE SELECTOR CHANGES cannot change owner in place, because a selector is immutable and most charts put
  # the release name in it. A Deployment or DaemonSet is deleted and the install creates it again. A StatefulSet keeps
  # its pods: it is deleted with --cascade=orphan, its pods are relabelled to the new selector, and the install's
  # StatefulSet adopts them and replaces them one at a time onto the same claims, so a clustered store (NATS JetStream)
  # keeps its quorum and its data. Each re-created workload is annotated keep after the install, as an adopted one is.
  OWN=()
  REPARENTED=()
  RENDER="$(mktemp)"
  trap 'rm -f "$RENDER"' EXIT
  "$HELM" template "$RELEASE" "${SOURCE[@]}" --namespace "$NAMESPACE" --include-crds "${VALUES[@]}" "${EXTRA[@]}" >"$RENDER"
  held="$( { kubectl get --namespace "$NAMESPACE" --ignore-not-found -o json -f "$RENDER" 2>/dev/null || true; } \
    | python3 -c '
import json, sys
release, render = sys.argv[1], sys.argv[2]

def items(raw):
    # kubectl get answers one resource as itself and several as a List; a client dry-run prints one document per
    # object, back to back.
    found, decoder, at = [], json.JSONDecoder(), 0
    raw = raw.strip()
    while raw.startswith("{", at):
        doc, at = decoder.raw_decode(raw, at)
        found += doc.get("items", []) if doc.get("kind") == "List" else [doc]
        while at < len(raw) and raw[at].isspace():
            at += 1
    return found

def selector(obj):
    labels = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items())) or "-"

with open(render) as f:
    # A dry-run that cannot map a kind (a CRD this fixture installs, on a cluster that does not serve it yet) prints `{}`
    # in its place: an object with no kind is one the cluster holds none of, so there is nothing to hand over.
    rendered = {(o["kind"], o["metadata"]["name"]): o for o in items(f.read()) if "kind" in o}
for item in items(sys.stdin.read()):
    meta = item["metadata"]
    owner = (meta.get("annotations") or {}).get("meta.helm.sh/release-name")
    if owner == release:
        continue
    new = rendered.get((item["kind"], meta["name"]), {})
    workload = item["kind"] in {"StatefulSet", "Deployment", "DaemonSet"}
    # A selector with no matchLabels is never relabelled: it names no pods a label can move.
    changed = "-" not in (selector(item), selector(new)) and selector(item) != selector(new)
    action = "reparent" if workload and new and changed else "adopt"
    # "-" for an absent field: `read` collapses runs of blanks, so an empty namespace would shift the owner left.
    print(item["kind"], meta["name"], meta.get("namespace") or "-", owner or "-", action, selector(item), selector(new))
' "$RELEASE" <(kubectl create --dry-run=client -o json -f "$RENDER" 2>/dev/null || echo '{}'))"
  rm -f "$RENDER"
  if [ -n "$held" ]; then
    while read -r kind obj ns owner action old new; do
      NS=()
      [ "$ns" != "-" ] && NS=(--namespace "$ns")
      if [ "$action" = "reparent" ]; then
        if [ "$kind" = "StatefulSet" ]; then
          kubectl delete "$kind" "$obj" "${NS[@]}" --cascade=orphan --wait=true >/dev/null
          # shellcheck disable=SC2086 # the selector's labels are one argument each
          kubectl label pods "${NS[@]}" -l "$old" ${new//,/ } --overwrite >/dev/null
          echo ">> $name: $kind/$obj (release $owner) re-created by release $RELEASE; its pods are adopted and rolled in place"
        else
          kubectl delete "$kind" "$obj" "${NS[@]}" --wait=true >/dev/null
          echo ">> $name: $kind/$obj (release $owner) re-created by release $RELEASE"
        fi
        REPARENTED+=("$kind/$obj/$ns/$old")
        continue
      fi
      kubectl annotate "$kind" "$obj" "${NS[@]}" helm.sh/resource-policy=keep --overwrite >/dev/null
      echo ">> $name: $kind/$obj (release $owner) kept and handed to release $RELEASE"
    done <<<"$held"
    # --force-conflicts too: an adopted object can carry fields its controller manages at runtime (Dapr's operator
    # owns its CRDs' conversion-webhook namespace), and Helm 4's server-side apply refuses those as conflicts. The
    # new owner's controller sets them again once it runs.
    OWN=(--take-ownership --force-conflicts)
  fi

  echo ">> fixture $name: ${SOURCE[*]} as release $RELEASE in $NAMESPACE"
  # A failed install (a pod that never turns Ready) still created the re-created workloads, and the old release's next
  # upgrade deletes any of them not yet marked keep (measured 2026-10-11: Dex's first install failed on its pod, the
  # next `make k3s-up` found the workload owned by the fixture and re-created nothing, and rask's upgrade deleted it).
  # So the marks below are written whatever the install's outcome, and its status is returned after them.
  # WAIT=false is for a stand-in whose pod needs something rask's own install creates (the object store's root credential,
  # which ESO writes from rask's dev store): on an empty cluster it starts once rask is installed.
  READY=(--wait --timeout 600s)
  [ "$WAIT" = "false" ] && READY=()
  installed=0
  "$HELM" upgrade --install "$RELEASE" "${SOURCE[@]}" \
    --namespace "$NAMESPACE" --create-namespace --reset-then-reuse-values "${VALUES[@]}" "${OWN[@]}" "${EXTRA[@]}" \
    --description "rask fixture: not for production" "${READY[@]}" || installed=$?
  for entry in "${REPARENTED[@]}"; do
    IFS=/ read -r kind obj ns old <<<"$entry"
    NS=()
    [ "$ns" != "-" ] && NS=(--namespace "$ns")
    if ! kubectl annotate "$kind" "$obj" "${NS[@]}" helm.sh/resource-policy=keep --overwrite >/dev/null; then
      echo "!! $name: could not mark $kind/$obj keep, so the old release's next upgrade deletes it" >&2
      [ "$installed" -ne 0 ] || installed=1
    fi
    # The orphaned StatefulSet's revisions carry its old selector, which the new one never adopts.
    if [ "$kind" = "StatefulSet" ]; then
      kubectl delete controllerrevisions "${NS[@]}" -l "$old" --ignore-not-found >/dev/null
    fi
  done
  [ "$installed" -eq 0 ] || exit "$installed"
  # A fixture in a namespace it shares with rask (SHARED_NAMESPACE=true) leaves the namespace unlabelled: the mark says
  # everything in it is throwaway, which rask's own namespace is not.
  if [ "$SHARED_NAMESPACE" != "true" ]; then
    kubectl label namespace "$NAMESPACE" "$MARK" --overwrite >/dev/null
  fi
done
