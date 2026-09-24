#!/usr/bin/env bash
# The Ray-path e2e stack + suite runner (#53) — the SINGLE definition used by BOTH `make e2e-ray-ci`
# and the CI `ray-e2e` job, mirroring scripts/e2e_stack.sh. It brings up the governed stack with the
# Ray-ON recipe and a real KubeRay head, then runs the two Ray-path suites so they cannot silently
# regress.
#
# WHY A SEPARATE JOB (not folded into e2e_stack.sh): the train head shares MEDALLION_RAY_ENABLED with
# the stage runners, so enabling it flips the whole cascade into stage-compute-via-ray. Flipping that ON mid-run
# against the openbao-ON core stack races the OpenBao secret store and hangs the stage runners (secret 500 →
# "waiting on port 8000"). This job instead deploys the Ray-ON recipe FROM THE START, and uses the
# governed-union recipe that is proven to work with ray on: openbao OFF (plaintext env secrets, so no
# secret race) + compute + ray + quality + auth + fga. Observability stays OFF to fit a 2-core/7 GB
# runner (the same reason e2e_stack.sh disables it); the train suite's GreptimeDB-metrics leg self-skips
# without LANCE_E2E_GREPTIME_URL while the pins + lineage + validator gates still run.
set -euo pipefail

CLUSTER="${CLUSTER:-rask-ray-e2e}"
RELEASE="${RELEASE:-rask}"
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
    # Cascade state: on a fixture timeout the stage runners stay Running (so the loop above misses them), yet the
    # cascade may have stalled at a Ray stage job. Dump the ignition + stage-submit trail + head job list.
    echo "--- cascade trail (medallion-producer ignition + stage runners' ray stage jobs) ---"
    for c in medallion-producer bronze-to-silver silver-to-gold; do
      echo "  [$c]"
      kubectl logs -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$c" \
        --all-containers --tail=60 2>/dev/null \
        | grep -iE "cascade|bronze_arrival|ray_stage_job|RETRY|quality|error|traceback|timeout" | tail -20 || true
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
# No --config: deploy/kind/kind-config.yaml pins `name: rask`, which conflicts with our own $CLUSTER
# name (and it only declares a single control-plane node — kind's default anyway).
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 180s
# THE CLUSTER THIS SCRIPT MEANS ([[XC-057]]). Without it every helm and kubectl call below inherits
# whatever the environment has, and `scripts/helm.sh` defaults that to the LIVE k3s estate — so this
# script created a kind cluster and then upgraded the live release instead, measured 2026-09-23.
# `kind export kubeconfig` writes the context; RASK_EXPECT_CONTEXT is what makes helm.sh refuse if
# anything later repoints it.
kind export kubeconfig --name "$CLUSTER"
export RASK_EXPECT_CONTEXT="kind-$CLUSTER"
# EVERY https REPOSITORY `chart/Chart.yaml` DECLARES, derived rather than listed. `helm dependency
# build` needs each one even when the component is disabled, and a hand-written list is a second copy
# of the chart's own dependency set: measured 2026-09-24, it named five of the nine, so the lane died
# on `no repository definition for https://nvidia.github.io/k8s-device-plugin,
# https://ray-project.github.io/kuberay-helm/` the first time it ran in five days. A tenth subchart
# cannot break this now. `oci://` repositories are skipped — helm resolves those without a repo add.
# Pinned by `tests/unit/test_the_e2e_stack_adds_every_chart_repository.py`.
while read -r url; do
  [ -n "$url" ] || continue
  name="$(printf '%s' "$url" | sed -E 's#^https?://([^./]+).*#\1#')"
  helm repo add "$name" "$url" >/dev/null 2>&1 || true
done <<EOF
$(grep -oE '^\s+repository:\s+https?://\S+' chart/Chart.yaml | awk '{print $2}' | sort -u)
EOF
helm repo update >/dev/null && helm dependency build ./chart >/dev/null
bash scripts/dagger-image.sh --name rest-catalog --tag "$CATALOG_IMG" >/dev/null
bash scripts/dagger-image.sh --name ray-lance --tag "$RAY_IMG" >/dev/null
kind load docker-image "$CATALOG_IMG" "$RAY_IMG" --name "$CLUSTER"

ALREADY_BUILT="$CATALOG_IMG $RAY_IMG"
HELM_SET=(
  --set image.localImages=true
  --set auth.enabled=true
  --set medallion.fgaEnabled=true
  --set medallion.compute=true
  --set medallion.ray=true
  --set medallion.quality=true
  --set catalog.warehouses.enabled=true
  --set observability.enabled=false
  --set maintenance.enabled=false
  --set frontend.enabled=false
)

# EVERY SIDE-LOADED IMAGE THE CHART SCHEDULES, derived from the render rather than a list kept here.
# `image.localImages=true` means a bare `<component>:dev` resolves on the NODE, so an image this
# script does not build cannot be pulled from anywhere: containerd asks Docker Hub for
# `docker.io/library/notifications:dev`, is told `pull access denied, repository does not exist`, and
# the pod sits in ImagePullBackOff forever. Measured 2026-09-24 on `e2e-stack`: the overlay schedules
# seven rask images and this script built one. A hand-kept list is exactly what drifted, so the list
# is the render's and a new service joins it by existing. `$HELM_SET` is the SAME array the upgrade
# below uses, so the set built and the set deployed cannot disagree.
SIDE_LOADED="$("$(dirname "$0")/helm.sh" template "$RELEASE" ./chart "${HELM_SET[@]}" \
  | { grep -oE '^[[:space:]]+image: [a-z0-9][a-z0-9-]*:dev$' || true; } | awk '{print $2}' | sort -u)"
# `|| true` ON THE GREP, THEN AN EXPLICIT CHECK. `set -o pipefail` is on, so a grep that matches
# nothing exits 1 and kills the script at this line with no message at all — a filter that stopped
# matching would read as an unexplained abort rather than as the thing it is.
if [ -z "$SIDE_LOADED" ]; then
  echo "!! the render named no side-loaded <component>:dev image — the filter or image.localImages has moved" >&2
  exit 1
fi
for img in $SIDE_LOADED; do
  case " $ALREADY_BUILT " in *" $img "*) continue ;; esac
  stem="${img%%:*}"; stem="${stem#lance-}"
  bash scripts/dagger-image.sh --name "$stem" --tag "$img" >/dev/null
  digest="$(docker image inspect --format '{{.Id}}' "$img")"
  kind load docker-image "$img" --name "$CLUSTER"
  if ! docker exec "${CLUSTER}-control-plane" crictl images -o json 2>/dev/null | grep -q "${digest#sha256:}"; then
    echo "!! kind node does not hold the freshly-built $img digest $digest after load — aborting" >&2
    exit 1
  fi
  echo "   node holds $img digest $digest"
done

step "2/6 deploy the governed Ray-ON stack (auth+fga+compute+ray+quality ON, openbao/observability/web OFF)"
# SIDE-LOADED, SO THE CHART HAS TO BE TOLD. The images above are built by Dagger and pushed into the
# kind node with `kind load docker-image` at `:dev`, which is exactly what `image.localImages` means:
# a bare `<component>:<tag>` reference that resolves on the NODE rather than at a registry. Without it
# `rask.image` refuses the render outright — `image.repository must be set to a registry ... or set
# image.localImages=true` — and the lane dies at `helm upgrade` having already built and loaded every
# image it needs. Measured 2026-09-24 on `e2e-ray`, the first run that got past the kubeconfig guard
# far enough to reach a deploy.
# A COMMENT CANNOT GO INSIDE THE INVOCATION: a `#` line inside a backslash continuation ends
# the command and turns every following line into its own, which `bash -n` accepts.
"$(dirname "$0")/helm.sh" upgrade --install "$RELEASE" ./chart --timeout 600s "${HELM_SET[@]}"
# Dapr sidecar-injector race + fresh-cluster recreate (see e2e_stack.sh for the full rationale).
kubectl rollout status deploy/dapr-sidecar-injector --timeout=300s
for d in catalog lineage medallion-producer bronze-to-silver silver-to-gold media-to-silver gateway; do
  kubectl delete pods -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$d" \
    --ignore-not-found >/dev/null 2>&1 || true
done
kubectl rollout status deploy/"$RELEASE"-openfga --timeout=300s
kubectl rollout status deploy/"$RELEASE"-catalog --timeout=300s
kubectl rollout status deploy/"$RELEASE"-lineage --timeout=300s
kubectl rollout status deploy/"$RELEASE"-medallion-producer --timeout=300s
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
kubectl port-forward "svc/$RELEASE-medallion-producer" 8002:8000 >/tmp/pf-ray.log 2>&1 & PF_PIDS+=($!)
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
# when the train suite's bronze→silver cascade (also Ray jobs) runs — avoids stacking cold-starts.
# The env vars MUST stay a contiguous command-prefix to `uv run pytest` (no comment splitting the `\`
# continuation) — a comment there ends the line, demoting them to non-exported shell vars the child
# pytest never sees, and every suite would skip "set LANCE_E2E_...".
LANCE_E2E_LANCERAY_URL=http://localhost:8002 LANCE_E2E_CATALOG_URL=http://localhost:2333 \
LANCE_E2E_AUTH_SERVER=http://localhost:2333 \
LANCE_E2E_LINEAGE_URL=http://localhost:8000 LANCE_E2E_DEX=http://localhost:5556/dex \
LANCE_E2E_FGA=http://localhost:8081 LANCE_E2E_DAPR_TOKEN="$DAPR_TOKEN" LANCE_E2E_GREPTIME_URL="" \
PYTHONPATH=services uv run pytest \
  tests/e2e-py/test_ray_batch_e2e.py \
  tests/e2e-py/test_ray_train_e2e.py \
  tests/e2e-py/test_governance_e2e.py \
  -v -rs -p no:cacheprovider | tee /tmp/e2e-ray.log
# No silent skips: the stack IS up, so a skip means a misconfigured env var, not "not applicable".
if grep -qE "[1-9][0-9]* skipped" /tmp/e2e-ray.log; then
  echo; echo "!! FAIL: a Ray-path suite SKIPPED against the live ray stack — misconfiguration:"
  grep -E "^SKIPPED|skipped" /tmp/e2e-ray.log || true
  exit 1
fi

echo
echo "✓ ray-path e2e green — both Ray paths proven on a fresh ray-on kind stack"
