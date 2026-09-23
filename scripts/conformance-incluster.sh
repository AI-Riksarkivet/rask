#!/usr/bin/env bash
# Run the spec-conformance suite as a Job INSIDE the cluster ([[LH-020]]).
#
# WHY THIS EXISTS. The catalog vends its own in-cluster address, so a host-side client receives a
# correct 900s credential for a host it cannot resolve. Measured 2026-09-19: the k3s service network
# IS routable from this host (`10.43.44.177` answers) and `rask-minio` does NOT resolve on it — the
# barrier is DNS, and it belongs to where the process runs rather than to the clients. So the lancedb
# and lance-ray cases skip from the host with that reason, and `make e2e-spec-conformance` reports a
# green that has not proved the byte read. Owner ruling 2026-09-19 (`docs/DECISIONS.md`): the
# conformance target runs from inside the cluster.
#
# NOT A REPLACEMENT for `make e2e-spec-conformance`. That one still proves resolution and credential
# issuance against a deployed catalog from a developer's own machine, which is the loop people use.
# This proves the READ, which only a process inside the cluster can.
#
# EVERY ADDRESS IS DISCOVERED from the release, never hardcoded, for `e2e_live.sh`'s reason: a script
# holding yesterday's address fails in a way that reads as a broken service.
#
# Usage:  bash scripts/conformance-incluster.sh
#         RELEASE=rask REGISTRY=172.17.0.1:5000 bash scripts/conformance-incluster.sh
set -euo pipefail

# THE CLUSTER THIS SCRIPT MEANS ([[XC-057]]). This one targets the deployed estate on purpose, and
# saying so is the point: an absent declaration and a deliberate one used to look identical, so
# "I meant the live cluster" was indistinguishable from "I never thought about it". Overridable, so a
# second estate (another host, another context) is a variable rather than an edit.
: "${RASK_EXPECT_CONTEXT:=default}"
export RASK_EXPECT_CONTEXT


RELEASE="${RELEASE:-rask}"
REGISTRY="${REGISTRY:-172.17.0.1:5000}"
# k3s pulls through localhost; Dagger pushes through the bridge gateway. Same registry, two names,
# because Dagger's engine is itself a container and `localhost` there is the engine.
PULL_REGISTRY="${PULL_REGISTRY:-localhost:5000}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$REPO/.localbin:$PATH"
JOB="conformance-$(date +%s)"

step() { printf '\n\033[1m>> %s\033[0m\n' "$*"; }
fail() { printf '\033[31m!! %s\033[0m\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || fail "kubectl is not on PATH"
kubectl get ns >/dev/null 2>&1 || fail "no reachable cluster (KUBECONFIG=$KUBECONFIG)"

step "1/4 building the conformance image with Dagger"
TAG="main-$(git -C "$REPO" rev-parse --short=8 HEAD)"
bash "$REPO/scripts/dagger-image.sh" --name conformance --tag "conformance:$TAG" --push "$REGISTRY/conformance:$TAG"

step "2/4 discovering the release's in-cluster addresses"
# THE NAMED PORT, not ports[0] — `e2e_live.sh` pays for that lesson with OpenFGA, whose gRPC port is
# published first. A Job addresses services by NAME, which is the whole point of running in-cluster.
svc_port() { kubectl get svc "$1" -o jsonpath='{.spec.ports[?(@.name=="'"$2"'")].port}' 2>/dev/null; }
CATALOG_PORT="$(svc_port "$RELEASE-catalog" http)"
[ -n "$CATALOG_PORT" ] || CATALOG_PORT="$(kubectl get svc "$RELEASE-catalog" -o jsonpath='{.spec.ports[0].port}')"
DEX_PORT="$(kubectl get svc "$RELEASE-dex" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
[ -n "$DEX_PORT" ] || fail "no $RELEASE-dex service — the suite mints a real bearer and cannot run without one"
CATALOG_URL="http://$RELEASE-catalog:$CATALOG_PORT"
DEX_URL="http://$RELEASE-dex:$DEX_PORT/dex"
echo "   catalog=$CATALOG_URL dex=$DEX_URL"

step "3/4 running the suite as a Job"
# `restartPolicy: Never` + `backoffLimit: 0`: a failing conformance run must FAIL, not be retried into
# a pass. `ttlSecondsAfterFinished` so repeated runs do not accumulate.
kubectl apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB
  labels: { app.kubernetes.io/name: conformance, app.kubernetes.io/part-of: $RELEASE }
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels: { app.kubernetes.io/name: conformance }
      # NO DAPR SIDECAR. A sidecar would never report ready for a Job that exits, leaving the pod
      # Running forever with the suite long finished.
      annotations: { dapr.io/enabled: "false" }
    spec:
      restartPolicy: Never
      securityContext: { runAsNonRoot: true, runAsUser: 10001, fsGroup: 10001 }
      containers:
        - name: conformance
          image: $PULL_REGISTRY/conformance:$TAG
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          env:
            - { name: LANCE_E2E_CATALOG_URL, value: "$CATALOG_URL" }
            - { name: LANCE_E2E_DEX, value: "$DEX_URL" }
          volumeMounts:
            - { name: tmp, mountPath: /tmp }
          resources:
            requests: { cpu: "500m", memory: "2Gi" }
            limits: { memory: "4Gi" }
      volumes:
        # Ray needs a session dir and pylance needs a spill store; `readOnlyRootFilesystem` with no
        # writable /tmp makes pylance 11 PANIC on opening ANY dataset (LocalSpillStore cannot create
        # its temp dir), which reads as a client fault rather than a container one.
        - name: tmp
          emptyDir: { sizeLimit: 2Gi }
YAML

step "4/4 waiting for the run"
kubectl wait --for=condition=complete --timeout=900s "job/$JOB" 2>/dev/null &
WAIT_OK=$!
kubectl wait --for=condition=failed --timeout=900s "job/$JOB" 2>/dev/null &
WAIT_FAIL=$!
wait -n "$WAIT_OK" "$WAIT_FAIL" || true
kubectl logs "job/$JOB" --tail=200 || true
kill "$WAIT_OK" "$WAIT_FAIL" 2>/dev/null || true

SUCCEEDED="$(kubectl get job "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null || echo 0)"
# A SKIP IS NOT A PASS, which is the entire reason this script exists. The suite's own guards skip on
# an unreachable vended endpoint, so an in-cluster run that still skips has proved nothing and must
# not exit 0 — that is the failure mode being fixed, reproduced one layer up.
if kubectl logs "job/$JOB" --tail=200 2>/dev/null | grep -qE '[0-9]+ skipped'; then
  fail "the in-cluster run SKIPPED a case — this target exists because a skip reads as green"
fi
[ "${SUCCEEDED:-0}" = "1" ] || fail "the conformance Job did not succeed"
printf '\n\033[32m== all three stock clients drove the deployed catalog from inside the cluster ==\033[0m\n'
