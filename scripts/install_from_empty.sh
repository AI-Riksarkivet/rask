#!/usr/bin/env bash
# Install rask the way a platform team does, onto the cluster KUBECONFIG names, which must be empty ([[XC-133]],
# ADR 0153): the development prerequisites and stand-ins from deploy/fixtures, each its own release, then the product
# chart, then wait until every workload is Ready. On failure it prints why: every pod's state, each failed container's
# exit reason, and the logs of whatever is not Ready, every Job pod's included.
#
# The images the render names (`<component>:$TAG`, image.localImages) must already be pullable by the cluster:
# `dagger call install-from-empty` builds them into a registry its k3s mirrors; scripts/e2e_stack.sh loads them into kind.
set -euo pipefail
cd "$(dirname "$0")/.."

RELEASE="${RELEASE:-rask}"
TAG="${TAG:-dev}"
TIMEOUT="${TIMEOUT:-900s}"
# shellcheck source=scripts/stack_values.sh
. scripts/stack_values.sh

JOB_LOGS="$(mktemp -d)"
RECORDER=""
diagnose() {
  local rc=$?
  [ -n "$RECORDER" ] && kill "$RECORDER" 2>/dev/null
  [ "$rc" -eq 0 ] && return 0
  echo "==> install-from-empty FAILED (exit $rc)"
  kubectl get pods -A -o wide || true
  kubectl get jobs -o wide || true
  kubectl get externalsecrets -o custom-columns='NAME:.metadata.name,READY:.status.conditions[0].status,REASON:.status.conditions[0].reason,MESSAGE:.status.conditions[0].message' 2>/dev/null || true
  for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[*].ready}{"\n"}{end}' | awk '/false/ || NF==1 {print $1}'); do
    echo "--- NOT READY: $p"
    kubectl describe pod "$p" 2>/dev/null | sed -n '/Events:/,$p' | head -15 || true
    kubectl logs "$p" --all-containers --tail=40 2>/dev/null || true
  done
  for f in "$JOB_LOGS"/*; do [ -f "$f" ] && { echo "--- JOB POD (recorded during the install): ${f##*/}"; cat "$f"; }; done
  return "$rc"
}
trap diagnose EXIT

echo "==> fixtures"
scripts/fixtures.sh external-secrets dapr nats dex openbao
# The object store runs the upstream RustFS image its fixture pins by digest, which the node pulls itself.
scripts/fixtures.sh rustfs

echo "==> rask"
# A Job that exhausts its backoff has its pod deleted, and that pod's log with it: record each Job pod's state and log
# (every container's, the attempt before a restart too) while the install runs.
( while sleep 3; do
    for p in $(kubectl get pods -l batch.kubernetes.io/job-name -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
      {
        kubectl get pod "$p" -o jsonpath='{range .status.initContainerStatuses[*]}{.name} restarts={.restartCount} last={.lastState.terminated.reason}/{.lastState.terminated.exitCode} now={.state.terminated.reason}/{.state.terminated.exitCode}{"\n"}{end}{range .status.containerStatuses[*]}{.name} restarts={.restartCount} last={.lastState.terminated.reason}/{.lastState.terminated.exitCode} now={.state.terminated.reason}/{.state.terminated.exitCode}{"\n"}{end}'
        for c in $(kubectl get pod "$p" -o jsonpath='{.spec.initContainers[*].name} {.spec.containers[*].name}'); do
          echo "[$c, previous attempt]"; kubectl logs "$p" -c "$c" --previous --tail=40 2>/dev/null || true
          echo "[$c]"; kubectl logs "$p" -c "$c" --tail=40 2>/dev/null || true
        done
      } > "$JOB_LOGS/$p.tmp" 2>&1 && mv "$JOB_LOGS/$p.tmp" "$JOB_LOGS/$p"
    done
  done ) &
RECORDER=$!
scripts/helm.sh upgrade --install "$RELEASE" ./chart --timeout "$TIMEOUT" "${HELM_SET[@]}"

echo "==> ready"
kubectl wait --for=condition=Available deploy --all --timeout="$TIMEOUT"
for s in $(kubectl get statefulsets -o name); do kubectl rollout status "$s" --timeout="$TIMEOUT"; done
kubectl get pods -o wide
echo "==> rask installed from empty: $(kubectl get deploy,statefulset --no-headers | wc -l) workloads Ready"
