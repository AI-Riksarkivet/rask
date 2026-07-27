#!/usr/bin/env bash
# The Ray-path e2e stack + suite runner (#53) — the SINGLE definition used by BOTH `make e2e-ray-ci`
# and the CI `ray-e2e` job, mirroring scripts/e2e_stack.sh. It brings up the governed stack with the
# Ray-ON recipe and a real KubeRay head, then runs the two Ray-path suites so they cannot silently
# regress.
#
# WHY A SEPARATE JOB (not folded into e2e_stack.sh): the train head shares MEDALLION_RAY_ENABLED with
# the movers, so enabling it flips the whole cascade into stage-compute-via-ray. Flipping that ON mid-run
# against the openbao-ON core stack races the OpenBao secret store and hangs the movers (secret 500 →
# "waiting on port 8000"). This job instead deploys the Ray-ON recipe FROM THE START, and uses the
# governed-union recipe that is proven to work with ray on: openbao OFF (plaintext env secrets, so no
# secret race) + compute + ray + quality + auth + fga. Observability stays OFF to fit a 2-core/7 GB
# runner (the same reason e2e_stack.sh disables it); the train suite's GreptimeDB-metrics leg self-skips
# without LANCE_E2E_GREPTIME_URL while the pins + lineage + validator gates still run.
set -euo pipefail

CLUSTER="${CLUSTER:-lance-ray-e2e}"
RELEASE="${RELEASE:-lance-ns}"
CATALOG_IMG="${CATALOG_IMG:-lance-rest-catalog:dev}"
RAY_IMG="${RAY_IMG:-ray-lance:dev}"
BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.localbin"
export PATH="$BIN:$PATH"

PF_PIDS=()
cleanup() {
  local rc=$?
  for pid in "${PF_PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  if [ $rc -ne 0 ]; then
    echo "::group::ray stack diagnostics (failure)"
    kubectl get pods -o wide || true
    for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' \
                 | awk '$2!="Running" && $2!="Succeeded" {print $1}'); do
      echo "--- NOT READY: $p"; kubectl describe pod "$p" 2>/dev/null | sed -n '/Events:/,$p' | head -15 || true
      kubectl logs "$p" --all-containers --tail=40 2>/dev/null | head -40 || true
    done
    # Cascade state: on a fixture timeout the movers stay Running (so the loop above misses them), yet the
    # cascade may have stalled at a Ray stage job. Dump the ignition + stage-submit trail + head job list.
    echo "--- cascade trail (lance-ray ignition + movers' ray stage jobs) ---"
    for c in lance-ray raw-to-bronze bronze-to-silver silver-to-gold; do
      echo "  [$c]"
      kubectl logs -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$c" \
        --all-containers --tail=60 2>/dev/null \
        | grep -iE "cascade|raw_arrival|ray_stage_job|RETRY|quality|error|traceback|timeout" | tail -20 || true
    done
    echo "--- ray-lance-head job list (SUCCEEDED/RUNNING/FAILED per stage) ---"
    kubectl exec deploy/ray-lance-head -- ray job list 2>/dev/null | tail -25 || true
    echo "::endgroup::"
  fi
  if [ "${KEEP_STACK:-0}" != "1" ] && [ "${CI:-}" = "true" ]; then
    kind delete cluster --name "$CLUSTER" 2>/dev/null || true
  fi
  return $rc
}
trap cleanup EXIT
step() { echo; echo "==> $*"; }

# A pod's `.status imageID` is the containerd MANIFEST digest; `docker image inspect .Id` is the CONFIG
# digest — different hashes for the same image. Match the pod's digest against the FULL crictl set for the
# tag (id + repoDigests); since `kind load` replaces the tag, any of them == the freshly-loaded image.
assert_fresh() {  # <image-tag> <pod-imageID>
  local tag="$1" running="$2" pod_sha node_shas
  pod_sha="${running##*:}"
  node_shas="$(docker exec "${CLUSTER}-control-plane" crictl images -o json 2>/dev/null | python3 -c "
import sys, json
shas = []
for img in json.load(sys.stdin).get('images', []):
    if any('$tag' in t for t in (img.get('repoTags') or [])):
        if img.get('id'): shas.append(img['id'].split(':')[-1])
        for rd in (img.get('repoDigests') or []): shas.append(rd.split(':')[-1])
print(' '.join(shas))
")"
  case " $node_shas " in
    *" $pod_sha "*) echo "   $tag pod serves the freshly-loaded image ($pod_sha)" ;;
    *) echo "!! $tag pod imageID ($running) not a digest the node holds — stale, aborting" >&2; exit 1 ;;
  esac
}

step "1/6 cluster + chart deps + images"
# No --config: deploy/kind/kind-config.yaml pins `name: lance`, which conflicts with our own $CLUSTER
# name (and it only declares a single control-plane node — kind's default anyway).
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 180s
# ALL declared chart dependencies' repos — `helm dependency build` needs every one even when the
# component is disabled (greptime/vector/perses are dependencies regardless of observability.enabled).
helm repo add dapr    https://dapr.github.io/helm-charts/          >/dev/null 2>&1 || true
helm repo add nats    https://nats-io.github.io/k8s/helm/charts/   >/dev/null 2>&1 || true
helm repo add openfga https://openfga.github.io/helm-charts        >/dev/null 2>&1 || true
helm repo add greptime https://greptimeteam.github.io/helm-charts/ >/dev/null 2>&1 || true
helm repo add vector  https://helm.vector.dev                      >/dev/null 2>&1 || true
helm repo add perses  https://perses.github.io/helm-charts         >/dev/null 2>&1 || true
helm repo update >/dev/null && helm dependency build ./chart >/dev/null
docker build -f .docker/rest-catalog.dockerfile -t "$CATALOG_IMG" . >/dev/null
docker build -f .docker/ray-lance.dockerfile -t "$RAY_IMG" . >/dev/null
kind load docker-image "$CATALOG_IMG" "$RAY_IMG" --name "$CLUSTER"

step "2/6 deploy the governed Ray-ON stack (auth+fga+compute+ray+quality ON, openbao/observability/web OFF)"
helm upgrade --install "$RELEASE" ./chart --timeout 600s \
  --set auth.enabled=true \
  --set medallion.fgaEnabled=true \
  --set medallion.compute=true \
  --set medallion.ray=true \
  --set medallion.quality=true \
  --set catalog.warehouses.enabled=true \
  --set openbao.enabled=false \
  --set observability.enabled=false \
  --set compaction.enabled=false \
  --set frontend.enabled=false
# Dapr sidecar-injector race + fresh-cluster recreate (see e2e_stack.sh for the full rationale).
kubectl rollout status deploy/dapr-sidecar-injector --timeout=300s
for d in catalog lineage lance-ray raw-to-bronze bronze-to-silver silver-to-gold media-to-silver gateway; do
  kubectl delete pods -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$d" \
    --ignore-not-found >/dev/null 2>&1 || true
done
kubectl rollout status deploy/"$RELEASE"-openfga --timeout=300s
kubectl rollout status deploy/"$RELEASE"-catalog --timeout=300s
kubectl rollout status deploy/"$RELEASE"-lineage --timeout=300s
kubectl rollout status deploy/"$RELEASE"-lance-ray --timeout=300s
RUNNING_ID="$(kubectl get pods -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=catalog" \
  -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="catalog")].imageID}')"
assert_fresh "$CATALOG_IMG" "$RUNNING_ID"

step "3/6 deploy the real KubeRay head + assert the fresh image"
kubectl apply -f deploy/ray-lance-demo.yaml
kubectl delete pods -l app=ray-lance-head --ignore-not-found >/dev/null 2>&1 || true
kubectl rollout status deploy/ray-lance-head --timeout=300s
RAY_RUNNING="$(kubectl get pods -l app=ray-lance-head -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="ray-head")].imageID}')"
assert_fresh "$RAY_IMG" "$RAY_RUNNING"

step "4/6 port-forward the services the suites talk to"
kubectl port-forward "svc/$RELEASE-lance-ray" 8002:8000 >/tmp/pf-ray.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-catalog"   2333:2333 >/tmp/pf-cat.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-lineage"   8000:8000 >/tmp/pf-lin.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-dex"       5556:5556 >/tmp/pf-dex.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-openfga"   8081:8080 >/tmp/pf-fga.log 2>&1 & PF_PIDS+=($!)
for i in $(seq 1 40); do
  curl -fsS -m2 -o /dev/null http://localhost:8002/livez 2>/dev/null \
    && curl -fsS -m2 -o /dev/null http://localhost:2333/readyz 2>/dev/null \
    && curl -fsS -m2 -o /dev/null http://localhost:8000/livez 2>/dev/null \
    && curl -fsS -m2 -o /dev/null http://localhost:8081/healthz 2>/dev/null \
    && break
  [ "$i" = "40" ] && { echo "!! services never became ready"; exit 1; }
  sleep 2
done

step "5/6 seed the medallion FGA grants (service identities the cascade + train run authenticate as)"
OPENFGA_API_URL=http://localhost:8081 scripts/seed_medallion_fga.sh || { echo "!! FGA seed failed"; exit 1; }
DAPR_TOKEN="$(kubectl get secret "$RELEASE-dapr-app-token" -o jsonpath='{.data.token}' | base64 -d)"
[ -n "$DAPR_TOKEN" ] || { echo "!! no dapr app token"; exit 1; }

step "6/6 run the two Ray-path suites against the live ray-on stack"
# Batch FIRST: its ray_lance_job submit cold-starts the Ray runtime env, so the ray cluster is warm
# when the train suite's raw→bronze→silver cascade (also Ray jobs) runs — avoids stacking cold-starts.
# The env vars MUST stay a contiguous command-prefix to `uv run pytest` (no comment splitting the `\`
# continuation) — a comment there ends the line, demoting them to non-exported shell vars the child
# pytest never sees, and every suite would skip "set LANCE_E2E_...".
LANCE_E2E_LANCERAY_URL=http://localhost:8002 LANCE_E2E_CATALOG_URL=http://localhost:2333 \
LANCE_E2E_AUTH_SERVER=http://localhost:2333 \
LANCE_E2E_LINEAGE_URL=http://localhost:8000 LANCE_E2E_DEX=http://localhost:5556/dex \
LANCE_E2E_FGA=http://localhost:8081 LANCE_E2E_DAPR_TOKEN="$DAPR_TOKEN" LANCE_E2E_GREPTIME_URL="" \
PYTHONPATH=services uv run pytest \
  tests/e2e/test_ray_batch_e2e.py \
  tests/e2e/test_ray_train_e2e.py \
  tests/e2e/test_governance_e2e.py \
  -v -rs -p no:cacheprovider | tee /tmp/e2e-ray.log
# No silent skips: the stack IS up, so a skip means a misconfigured env var, not "not applicable".
if grep -qE "[1-9][0-9]* skipped" /tmp/e2e-ray.log; then
  echo; echo "!! FAIL: a Ray-path suite SKIPPED against the live ray stack — misconfiguration:"
  grep -E "^SKIPPED|skipped" /tmp/e2e-ray.log || true
  exit 1
fi

echo
echo "✓ ray-path e2e green — both Ray paths proven on a fresh ray-on kind stack"
