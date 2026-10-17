#!/usr/bin/env bash
# The e2e stack + suite runner — the SINGLE definition used by BOTH `make e2e-ci` and the CI job,
# so "it passed in CI" and "it passed on my machine" cannot diverge (the repo's dagger/auth-e2e pattern).
#
# WHY THIS EXISTS (docs/adr/0001-p0-1-why-e2e-stack-sh-exists-live-verify-honesty.md P0.1): every "live-verified" claim in this repo rested on a
# MANUAL terminal run while `ci.yml` ran `pytest -m "not e2e"`. Nothing guarded them, so a regression
# tomorrow would be silent. This script makes the live proof an artifact that runs on every push.
#
# It brings up the GOVERNED stack (Dex OIDC + OpenFGA + Dapr + RustFS + AGE) on kind with the feature
# flags the suites need, seeds the exact grants/buckets they assume, then runs the five e2e suites the
# goal names: CAS, client-direct (#2), warehouses (#3-A), multibase (#3-B), outbox (#4).
#
# Heavy optional components (observability: the OTel Collector, web, maintenance) are DISABLED — they
# are not under test here and a GitHub runner is 2 cores / 7 GB. `make e2e-obs` covers observability.
#
# Idempotent: safe to re-run against an existing cluster. Env overrides: CLUSTER, RELEASE, KEEP_STACK=1.
set -euo pipefail

CLUSTER="${CLUSTER:-rask}"
RELEASE="${RELEASE:-rask}"
CATALOG_IMG="${CATALOG_IMG:-lance-rest-catalog:dev}"
TAG="${CATALOG_IMG##*:}"
BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.localbin"
export PATH="$BIN:$PATH"


PF_PIDS=()
cleanup() {
  local rc=$?
  for pid in "${PF_PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  if [ $rc -ne 0 ]; then
    echo "::group::stack diagnostics (failure)"
    kubectl get pods -o wide || true
    # Dump EVERY not-ready pod, not a hardcoded list. The hardcoded catalog+lineage dump is why two 12-minute
    # CI round-trips told us nothing: the pod actually blocking the stack was OpenFGA, and we never looked at
    # it. A diagnostic that only reports the components you already suspected cannot find a surprise.
    for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' \
                 | awk '$2!="Running" && $2!="Succeeded" {print $1}'); do
      echo "--- NOT READY: $p"
      kubectl describe pod "$p" 2>/dev/null | sed -n '/Events:/,$p' | head -15 || true
    done
    for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
      ready=$(kubectl get pod "$p" -o jsonpath='{.status.containerStatuses[*].ready}' 2>/dev/null)
      case "$ready" in *false*)
        echo "--- LOGS (not-ready containers): $p"
        kubectl logs "$p" --all-containers --tail=40 2>/dev/null | head -40 || true ;;
      esac
    done
    kubectl get jobs -o wide || true   # the OpenFGA migration is a post-install HOOK job — surface its state
    # A failed hook Job's pod is gone by now, so the not-ready loop above never prints its log, and that log is the only
    # account of why it failed: the install recorded it.
    for f in "${JOB_LOGS:-/nonexistent}".*; do
      [ -f "$f" ] && { echo "--- LOGS (job pod, recorded during the install): ${f##*.}"; cat "$f"; }
    done
    # Every credential a pod mounts arrives through an ExternalSecret from the dev store: a Secret that never appeared is
    # a condition here, and the store's seed log says what it wrote.
    kubectl get externalsecrets -o custom-columns='NAME:.metadata.name,READY:.status.conditions[0].status,REASON:.status.conditions[0].reason,MESSAGE:.status.conditions[0].message' 2>/dev/null || true
    kubectl get secretstores -o wide 2>/dev/null || true
    echo "--- LOGS: the dev store's seed"
    kubectl logs "deploy/${RELEASE}-openbao" -c seed --tail=60 2>/dev/null || true
    echo "::endgroup::"
  fi
  # WHERE THE STACK'S TIME WENT, on every run: the lane's budget is set by its slowest bring-up, and a pull, a restart
  # and a dependency wait all read as one long `helm upgrade` unless each is printed. kubelet's `Pulled` event names the
  # pull's own duration; a pod's restarts and its created-to-Ready span name the waits.
  echo "::group::stack timeline (image pulls, restarts, created-to-Ready)"
  kubectl get events -A --field-selector reason=Pulled -o jsonpath='{range .items[*]}{.metadata.namespace}/{.involvedObject.name}: {.message}{"\n"}{end}' 2>/dev/null \
    | grep -v 'already present' || true
  kubectl get pods -A -o json 2>/dev/null | python3 -c '
import json, sys
from datetime import datetime
ts = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
for p in json.load(sys.stdin)["items"]:
    meta, status = p["metadata"], p.get("status", {})
    created = meta["creationTimestamp"]
    ready = next((c["lastTransitionTime"] for c in status.get("conditions", []) if c["type"] == "Ready" and c["status"] == "True"), None)
    restarts = sum(c.get("restartCount", 0) for c in status.get("containerStatuses", []))
    took = "%ds to Ready" % (ts(ready) - ts(created)).total_seconds() if ready else "not Ready now"
    last = ["%s=%s/%s" % (c["name"], t.get("reason"), t.get("exitCode")) for c in status.get("containerStatuses", []) if (t := c.get("lastState", {}).get("terminated"))]
    print("%s/%s: created %s, %s, restarts=%d %s" % (meta["namespace"], meta["name"], created, took, restarts, " ".join(last)))
' || true
  # What a restarted container said before it died: the reason a bring-up waited.
  for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[*].restartCount}{"\n"}{end}' 2>/dev/null \
               | awk '{for (i = 2; i <= NF; i++) if ($i > 0) {print $1; break}}'); do
    echo "--- previous run of $p"
    kubectl logs "$p" --all-containers --previous --tail=5 2>/dev/null || true
  done
  echo "::endgroup::"
  if [ "${KEEP_STACK:-0}" != "1" ] && [ "${CI:-}" = "true" ]; then
    kind delete cluster --name "$CLUSTER" 2>/dev/null || true
  fi
  return $rc
}
trap cleanup EXIT

step() { echo; echo "==> $*"; }

# --------------------------------------------------------------------------------------------------
step "1/9 chart deps + the images the render schedules (built while the cluster comes up)"
# EVERY https REPOSITORY `chart/Chart.yaml` DECLARES, derived rather than listed. `helm dependency
# build` needs each one even when the component is disabled, and a hand-written list is a second copy
# of the chart's own dependency set: measured 2026-09-24, it named five of the nine, so the lane died
# on `no repository definition for https://nvidia.github.io/k8s-device-plugin,
# https://ray-project.github.io/kuberay-helm/` the first time it ran in five days. A tenth subchart
# cannot break this now. `oci://` repositories are skipped — helm resolves those without a repo add.
while read -r url; do
  [ -n "$url" ] || continue
  name="$(printf '%s' "$url" | sed -E 's#^https?://([^./]+).*#\1#')"
  helm repo add "$name" "$url" >/dev/null 2>&1 || true
done <<EOF
$(grep -oE '^\s+repository:\s+https?://\S+' chart/Chart.yaml | awk '{print $2}' | sort -u)
EOF
helm repo update >/dev/null && helm dependency build ./chart >/dev/null

# The governed stack's values, shared with scripts/install_from_empty.sh so the two lanes install one shape.
# shellcheck source=scripts/stack_values.sh
. "$(dirname "$0")/stack_values.sh"
# FLOWS OFF: no suite this lane runs reaches it, and on a 4-core runner its build and sidecar pod are what the lane's
# budget goes to (measured 2026-10-16: with flows and notifications on, the fixture installs ran beside four builds and
# Dapr's took 90 s instead of 13 s). Scaled to zero, the render schedules no pod for it, so the side-loaded set below
# asks for no flows image; install_from_empty keeps it, since it proves every workload starts. NOTIFICATIONS STAYS ON:
# criterion 4's module reads the inboxes the shared scenario fills (tests/e2e-py/test_events_are_correct.py).
#
# A VENDED CREDENTIAL NAMES THE ADDRESS THE RUNNER REACHES THE STORE AT, the port-forward below: the scenario's stock
# client writes from this runner to the endpoint the catalog vends, and the catalog's own `rask-minio:9000` resolves
# only in the cluster (`vending.endpoint`, LH-489). Every other suite here brings its own storage options.
HELM_SET+=(--set services.flows.replicas=0 --set-string vending.endpoint=http://localhost:9900)

# EVERY SIDE-LOADED IMAGE THE CHART SCHEDULES, derived from the render rather than a list kept here.
# `image.localImages=true` means a bare `<component>:dev` resolves on the NODE, so an image this
# script does not build cannot be pulled from anywhere: containerd asks Docker Hub for
# `docker.io/library/notifications:dev`, is told `pull access denied, repository does not exist`, and
# the pod sits in ImagePullBackOff forever. Measured 2026-09-24 on `e2e-stack`: the overlay schedules
# seven rask images and this script built one. A hand-kept list is exactly what drifted, so the list
# is the render's and a new service joins it by existing. `$HELM_SET` is the SAME array the upgrade
# below uses, so the set built and the set deployed cannot disagree.
# EXTRACTED BY PARSING THE RENDER, not by matching its text: every chart call site passes
# `rask.image` through `quote`, so a reference arrives here as `image: "notifications:dev"` and a filter
# anchored on the unquoted scalar matches none of them. `scripts/side_loaded_images.py` is that
# parse, shared with the gate that pins it, and it emits the dockerfile stem beside each image so
# this loop needs to know neither rule. `--tag` comes from $CATALOG_IMG so one variable decides the
# tag this script builds and the tag it looks for.
SIDE_LOADED="$("$(dirname "$0")/helm.sh" template "$RELEASE" ./chart "${HELM_SET[@]}" \
  --api-versions external-secrets.io/v1/ExternalSecret --api-versions generators.external-secrets.io/v1alpha1/Password \
  --api-versions dapr.io/v1alpha1 \
  | uv run python "$(dirname "$0")/side_loaded_images.py" --tag "$TAG")"
# AN EXPLICIT CHECK, because an empty set is not an empty amount of work — it means this script is
# about to deploy images it never built, and every pod that needs one sits in ImagePullBackOff.
if [ -z "$SIDE_LOADED" ]; then
  echo "!! the render named no side-loaded <component>:$TAG image — image.localImages or image.tag has moved" >&2
  exit 1
fi

# BUILT AS TARBALLS, IN THE BACKGROUND. No build needs the cluster, so they run while kind and the fixtures come up, and
# `--out` hands each one over as an OCI archive the node imports directly: no image passes through a docker daemon.
IMAGE_DIR="$(mktemp -d)"
IMAGE_CACHE="${E2E_IMAGE_CACHE:-$HOME/.cache/rask-e2e-images}"
mkdir -p "$IMAGE_CACHE"
build_image() {
  local img="$1" stem="$2" dockerfile=".docker/$2.dockerfile" kept=""
  # A dockerfile that copies nothing from the build context is a function of its own text and the pinned sources it
  # names, so its archive is kept across runs under the hash of that text and of the script that writes the archive
  # (`E2E_IMAGE_CACHE`, which CI restores). Every other image is built from this checkout, every run.
  if ! grep -vE -- '--from=' "$dockerfile" | grep -qE '^[[:space:]]*(COPY|ADD)[[:space:]]'; then
    kept="$IMAGE_CACHE/$stem-$(cat "$dockerfile" scripts/dagger-image.sh | sha256sum | cut -c1-16).tar"
    echo "$kept" >>"$IMAGE_DIR/kept"
    if [ -f "$kept" ]; then
      cp "$kept" "$IMAGE_DIR/$stem.tar"
      echo ">> $img: kept build of $dockerfile reused ($kept)"
      return
    fi
  fi
  bash scripts/dagger-image.sh --name "$stem" --tag "$img" --out "$IMAGE_DIR/$stem.tar"
  [ -z "$kept" ] || cp "$IMAGE_DIR/$stem.tar" "$kept"
}
( while read -r img stem; do
    [ -n "$img" ] || continue
    build_image "$img" "$stem" || exit 1
  done <<<"$SIDE_LOADED" ) >"$IMAGE_DIR/build.log" 2>&1 & BUILD_PID=$!
PF_PIDS+=("$BUILD_PID")

step "2/9 cluster + platform fixtures"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --config deploy/kind/kind-config.yaml --wait 180s
# THE CLUSTER THIS SCRIPT MEANS ([[XC-057]]). Without it every helm and kubectl call below inherits
# whatever the environment has, and `scripts/helm.sh` defaults that to the LIVE k3s estate — so this
# script created a kind cluster and then upgraded the live release instead, measured 2026-09-23.
# `kind export kubeconfig` writes the context; RASK_EXPECT_CONTEXT is what makes helm.sh refuse if
# anything later repoints it.
kind export kubeconfig --name "$CLUSTER"
export RASK_EXPECT_CONTEXT="kind-$CLUSTER"
# THE EXTERNAL SECRETS OPERATOR, DAPR AND THE NATS BUS, platform prerequisites the chart refuses to render without
# ([[XC-004]], [[XC-128]], [[XC-129]]). This throwaway cluster stands in for the platform, so the lane installs them
# from the same fixtures `make k3s-up` uses (deploy/fixtures/external-secrets, dapr, nats), each on its own release,
# before rask. fixtures.sh waits for each, so Dapr's injector is Ready before any rask pod exists. The bus runs with
# operator mode off (the fixture's default), so it takes the credentials rask's clients present without checking them.
# Dex, the dev OIDC issuer the lane mints its tokens from, the dev object store and the dev secret store are fixtures too
# ([[XC-131]]). The two stores start once rask is installed (the object store's root credential and the secret store's
# seed scripts are rask's), so fixtures.sh does not wait for them.
"$(dirname "$0")/fixtures.sh" external-secrets dapr nats dex openbao

step "3/9 side-load the images"
wait "$BUILD_PID" || { cat "$IMAGE_DIR/build.log"; echo "!! an image build failed (log above)" >&2; exit 1; }
grep '^>> ' "$IMAGE_DIR/build.log" || true
# A kept build whose dockerfile has since changed is never asked for again; drop it so the kept set stays this run's.
for f in "$IMAGE_CACHE"/*.tar; do
  if [ -f "$f" ] && ! grep -qxF "$f" "$IMAGE_DIR/kept" 2>/dev/null; then rm -f "$f"; fi
done
# EXPLICIT import + digest record: the same-":dev"-tag gotcha (a rebuilt tag does NOT update a running pod under
# IfNotPresent) has bitten this repo repeatedly. Each archive is named by `dagger-image.sh --tag`, so kind imports it
# under the name the render asks for; its config and manifest digests are recorded here and every running container
# is held to them after the rollout, so a silent no-op import cannot serve old code under the new tag.
FRESH="$(mktemp)"
while read -r img stem; do
  [ -n "$img" ] || continue
  digests="$(python3 -c '
import json, sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    config = json.load(archive.extractfile("manifest.json"))[0]["Config"].rsplit("/", 1)[1]
    manifest = json.load(archive.extractfile("index.json"))["manifests"][0]["digest"].split(":", 1)[1]
print(config, manifest)' "$IMAGE_DIR/$stem.tar")"
  kind load image-archive "$IMAGE_DIR/$stem.tar" --name "$CLUSTER" >/dev/null
  echo "$img $digests" >>"$FRESH"
  echo "   loaded $img (image id ${digests:0:12}…)"
done <<<"$SIDE_LOADED"
rm -rf "$IMAGE_DIR"
# The object store runs rask's first-party image, imported above under the name rask's own hooks use. A `--set` beats
# the estate's values-live-pins.yaml, a tag in a registry this cluster cannot reach.
"$(dirname "$0")/fixtures.sh" minio -- --set-string image.repository=minio --set-string "image.tag=$TAG"

step "4/9 deploy the governed stack (auth ON, #3-A/#3-B/#4 flags ON, heavy extras OFF)"
# HISTORY: `--wait` used to DEADLOCK on a fresh cluster, which is why this job never once passed in CI.
# helm's order is: apply manifests → (--wait) block until every resource is Ready → run post-install hooks.
# The OpenFGA schema migration WAS a post-install hook, and the OpenFGA server cannot become Ready until its
# schema exists. So --wait blocked on OpenFGA, OpenFGA blocked on the migration, and the migration blocked on
# --wait. It "worked" on a long-lived local cluster only because a PREVIOUS install had already migrated the
# database, so the server came up Ready immediately and the deadlock never armed. Textbook works-on-my-machine.
# FIXED IN THE CHART (2026-07-28): the four bootstrap Jobs are ordinary release resources now, applied in the
# same wave as the servers they unblock — see "BOOTSTRAP JOBS" in chart/templates/_helpers.tpl. `helm install
# --wait` is safe with no wrapper. This script still omits it, for a DIFFERENT and still-real reason: the Dapr
# sidecar-injector race below means the app pods that come up in the first wave are recreated anyway, so
# waiting on them here would only buy a wait we then throw away. The explicit rollout waits are the ones that
# matter. Do not re-read this as "the deadlock is still there".
# SIDE-LOADED, SO THE CHART HAS TO BE TOLD. The images above are built by Dagger and imported into the
# kind node's containerd at `:dev`, which is exactly what `image.localImages` means:
# a bare `<component>:<tag>` reference that resolves on the NODE rather than at a registry. Without it
# `rask.image` refuses the render outright — `image.repository must be set to a registry ... or set
# image.localImages=true` — and the lane dies at `helm upgrade` having already built and loaded every
# image it needs. Measured 2026-09-24 on `e2e-ray`, the first run that got past the kubeconfig guard
# far enough to reach a deploy.
# A COMMENT CANNOT GO INSIDE THE INVOCATION: a `#` line inside a backslash continuation ends
# the command and turns every following line into its own, which `bash -n` accepts.
# A Job that exhausts its backoff has its pod deleted, and that pod's log with it: record every Job pod's log (the attempt
# before a restart too) while the install runs, for the failure diagnostics to print.
JOB_LOGS="$(mktemp)"
( while sleep 5; do
    for p in $(kubectl get pods -l batch.kubernetes.io/job-name -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
      { echo "=== $p $(date -u +%T)"
        kubectl get pod "$p" -o jsonpath='{range .status.initContainerStatuses[*]}{.name} restarts={.restartCount} last={.lastState.terminated.reason}/{.lastState.terminated.exitCode} now={.state.terminated.reason}/{.state.terminated.exitCode}{"\n"}{end}{range .status.containerStatuses[*]}{.name} restarts={.restartCount} last={.lastState.terminated.reason}/{.lastState.terminated.exitCode} now={.state.terminated.reason}/{.state.terminated.exitCode}{"\n"}{end}' 2>/dev/null
        kubectl logs "$p" --all-containers --previous --tail=40 2>/dev/null
        kubectl logs "$p" --all-containers --tail=40 2>/dev/null; } > "$JOB_LOGS.$p" 2>&1 || true
    done
  done ) & PF_PIDS+=($!)
# THE DEV STORE COMES UP AFTER THE SECRETS THAT READ IT. The dev OpenBao is seeded by scripts this release renders, so
# every ExternalSecret and the SecretStore exist before the store can answer, and each first sync fails. ESO retries a
# failure on a doubling backoff (1 s base, 7 min cap, `pkg/controllers/common.BuildRateLimiter`), an ExternalSecret
# syncs only once its store reads Ready (`secretstore.assertStoreIsUsable`), and nothing wakes either when the store
# becomes usable — ESO v2.10.0 does not watch the store from the ExternalSecret controller. So a Secret appeared at the
# next doubling step after the store was ready: measured 2026-10-16, AGE waited 74 s on one run and 267 s on the next for
# the same store. A platform's store exists before rask installs, so this is the dev bring-up's alone. Once the store's
# own readiness says it is seeded, the lane wakes both controllers through ESO's update watch and its `force-sync`.
( kubectl wait "deploy/$RELEASE-openbao" --for=condition=Available --timeout=600s >/dev/null 2>&1 || exit 0
  kubectl annotate secretstore --all "rask.dev/store-seeded=$(date +%s)" --overwrite >/dev/null 2>&1
  kubectl wait secretstore --all --for=condition=Ready --timeout=120s >/dev/null 2>&1
  kubectl annotate externalsecret --all "external-secrets.io/force-sync=$(date +%s)" --overwrite >/dev/null 2>&1
  echo "   dev store seeded at $(date -u +%T): its SecretStore revalidated and every ExternalSecret re-synced" ) & PF_PIDS+=($!)
"$(dirname "$0")/helm.sh" upgrade --install "$RELEASE" ./chart --timeout 600s "${HELM_SET[@]}"

# Recreate the app pods. Pod DELETE, not `rollout restart`: on a re-run against an existing cluster a rebuilt
# same-tag image only lands on a freshly-scheduled pod, and delete forces that reschedule to pull the just-loaded
# digest from the node cache (IfNotPresent) — the kind same-tag staleness fix (memory: kind-same-tag-image-gotcha).
for d in catalog lineage notifications; do
  kubectl delete pods -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$d" \
    --ignore-not-found >/dev/null 2>&1 || true
done

# Explicit readiness, in dependency order. OpenFGA FIRST: its post-install migration must land before the
# server can serve, and every governed app fails closed without it — so if it is not up, everything
# downstream crash-loops and the real cause is buried under a pile of secondary failures.
kubectl rollout status deploy/"$RELEASE"-openfga --timeout=300s
kubectl rollout status deploy/"$RELEASE"-catalog --timeout=300s
kubectl rollout status deploy/"$RELEASE"-lineage --timeout=300s
kubectl rollout status deploy/"$RELEASE"-notifications --timeout=300s
# A ROLLED-OUT SUBSCRIBER IS NOT YET A LISTENING ONE: its sidecar registers its subscriptions and the inbox actors'
# host after the pod is Ready, and the inbox suite needs both. The sidecar's own `/v1.0/metadata` says when, as
# scripts/e2e_live.sh's settle gate reads it.
for i in $(seq 1 60); do
  kubectl exec "deploy/$RELEASE-notifications" -c notifications -- python -c '
import json, sys, urllib.request
m = json.load(urllib.request.urlopen("http://127.0.0.1:3500/v1.0/metadata", timeout=5))
sys.exit(0 if {s.get("topic") for s in (m.get("subscriptions") or [])} and (m.get("actorRuntime") or {}).get("hostReady") else 1)
' >/dev/null 2>&1 && { echo "   notifications' sidecar subscribed and hosting its actors"; break; }
  [ "$i" = "60" ] && { echo "!! notifications' sidecar never registered its subscriptions and actors"; exit 1; }
  sleep 3
done

# Every running container of a side-loaded image must serve the archive this run built — the definitive proof the whole
# suite tests fresh code, not a stale same-tag image (the failure mode this guard exists for). A container's `imageID`
# names either digest depending on how containerd recorded the import, so both recorded above are accepted.
kubectl get pods -o json >"$FRESH.pods"
python3 -c '
import json, sys
fresh = {line.split()[0]: set(line.split()[1:]) for line in open(sys.argv[1]) if line.strip()}
checked, stale = 0, []
for pod in json.load(open(sys.argv[2]))["items"]:
    for container in pod.get("status", {}).get("containerStatuses", []):
        image = container["image"].removeprefix("docker.io/library/")
        if image not in fresh or not container.get("imageID"):
            continue
        checked += 1
        if container["imageID"].rsplit(":", 1)[-1] not in fresh[image]:
            stale.append("%s/%s: %s is not the %s this run built" % (pod["metadata"]["name"], container["name"], container["imageID"], image))
if stale or not checked:
    sys.exit("!! " + ("; ".join(stale) or "no running container serves a side-loaded image"))
print(f"   {checked} running containers serve the images this run built")
' "$FRESH" "$FRESH.pods"

step "5/9 port-forward the services the suites talk to"
# A FORWARD FOLLOWS ITS SERVICE'S READY POD. `kubectl port-forward` binds to one pod for its life, and when that pod goes
# the forward can stay up and drop every connection it accepts (measured on this lane: `RemoteDisconnected` on every call
# through lineage's forward once a drill had scaled lineage down and back). Criterion 5's drills restart the pods behind
# six of these, so each forward names the pod it binds, and is restarted once that pod is no longer a ready endpoint of
# its Service. The loop kills its forward when the lane's cleanup stops it.
ready_pods() {
  kubectl get endpointslices -l "kubernetes.io/service-name=$1" -o json 2>/dev/null | python3 -c '
import json, sys
slices = json.load(sys.stdin).get("items") or []
print(" ".join(e["targetRef"]["name"] for s in slices for e in s.get("endpoints") or [] if (e.get("conditions") or {}).get("ready") and e.get("targetRef")))'
}
forward() {
  local service="$1" ports="$2" log="$3"
  ( trap 'kill "$child" 2>/dev/null; exit 0' TERM
    child=""
    while :; do
      pod="$(ready_pods "$service" | awk '{print $1}')"
      if [ -z "$pod" ]; then sleep 1; continue; fi
      kubectl port-forward "pod/$pod" "$ports" >>"$log" 2>&1 & child=$!
      while kill -0 "$child" 2>/dev/null && ready_pods "$service" | tr ' ' '\n' | grep -qx "$pod"; do sleep 2; done
      kill "$child" 2>/dev/null || true
      wait "$child" 2>/dev/null || true
    done ) & PF_PIDS+=($!)
}
forward "$RELEASE-catalog" 2333:2333 /tmp/pf-cat.log
forward "$RELEASE-lineage" 18000:8000 /tmp/pf-lin.log
# `minio`, not `rustfs`. The chart has rendered the object store as StatefulSet + Service
# `rask-minio` since 2026-09-11; nothing renders `-rustfs` any more, so this port-forward addressed an
# object that does not exist. Verified against the running estate rather than the templates: `svc/rask-minio` and
# `statefulset.apps/rask-minio`.
forward "$RELEASE-minio" 9900:9000 /tmp/pf-rfs.log
forward "$RELEASE-dex" 5556:5556 /tmp/pf-dex.log
forward "$RELEASE-openfga" 8081:8080 /tmp/pf-fga.log
forward "$RELEASE-notifications" 8850:8850 /tmp/pf-not.log
# The bus `nats.url` names (stack_values.sh): criterion 4's module reads its streams and publishes on it.
forward rask-nats 4222:4222 /tmp/pf-nats.log
for i in $(seq 1 40); do
  c=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:2333/livez 2>/dev/null || true)
  l=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:18000/livez 2>/dev/null || true)
  d=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:5556/dex/.well-known/openid-configuration 2>/dev/null || true)
  [ "$c" = "200" ] && [ "$l" = "200" ] && [ "$d" = "200" ] && break
  sleep 2
done
echo "   catalog=$c lineage=$l dex=$d"
[ "$c" = "200" ] && [ "$l" = "200" ] && [ "$d" = "200" ] || { echo "!! services never became ready"; exit 1; }

step "6/9 provision the #3-B data buckets (the allowlist GOVERNS, it does not provision)"
# THE STORE'S OWN ROOT PAIR, from the Secret its StatefulSet reads. The store is seeded with a generated secret, so a
# literal `minioadmin` signs every request with the wrong key: the store answers SignatureDoesNotMatch, the data
# buckets are never made, and the suites that reach the store directly report it unreachable.
S3_KEY="$(kubectl get secret "$RELEASE-infra-credentials" -o jsonpath='{.data.minio-access-key}' | base64 -d)"
S3_SECRET="$(kubectl get secret "$RELEASE-infra-credentials" -o jsonpath='{.data.minio-secret-key}' | base64 -d)"
[ -n "$S3_KEY" ] && [ -n "$S3_SECRET" ] || { echo "!! $RELEASE-infra-credentials holds no object-store pair"; exit 1; }
S3_KEY="$S3_KEY" S3_SECRET="$S3_SECRET" uv run python - "${BASE_A#s3://}" "${BASE_B#s3://}" <<'PYEOF'
import os, sys
import boto3
from botocore.exceptions import ClientError
c = boto3.client("s3", endpoint_url="http://localhost:9900", aws_access_key_id=os.environ["S3_KEY"],
                 aws_secret_access_key=os.environ["S3_SECRET"], region_name="us-east-1")
for b in sys.argv[1:]:
    try:
        c.create_bucket(Bucket=b)
        print("   created", b)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
        print("   exists", b)
PYEOF

step "7/9 mint Dex tokens + build the tenant through the catalog's doors"
mint() {
  curl -s -m10 http://localhost:5556/dex/token \
    -d grant_type=password -d client_id=lance-catalog -d client_secret=lance-catalog-secret \
    -d scope="openid email federated:id" -d username="$1" -d password=password \
  | uv run python -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"
}
ALICE="$(mint alice@example.com)"
BOB="$(mint bob@example.com)"
[ -n "$ALICE" ] && [ -n "$BOB" ] || { echo "!! Dex issued no token"; exit 1; }

# The FGA subject is the principal the token proves (`<idp-id>~<user id>`, LH-063), NOT the email — granting the
# email authorizes nobody. The catalog names it: `/management/v1/me` answers the principal its connector map derives.
principal_of() {
  curl -s -m 30 http://localhost:2333/management/v1/me -H "authorization: Bearer $1" \
  | uv run python -c "import sys,json;print(json.load(sys.stdin).get('sub',''))"
}
PRINCIPAL="$(principal_of "$ALICE")"
BOB_PRINCIPAL="$(principal_of "$BOB")"
# The install named the estate admin from the Dex fixture before Dex existed (stack_values.sh); the catalog says who
# Dex's token proves. A mismatch is an estate whose admin is nobody the suites can log in as.
[ "$PRINCIPAL" = "$ADMIN_PRINCIPAL" ] || { echo "!! auth.bootstrapAdmin=$ADMIN_PRINCIPAL but alice's token proves $PRINCIPAL"; exit 1; }
# The OpenFGA user a principal is, through the one seam every tuple writer uses.
ALICE_USER="$(uv run python -c "import sys; from service_kit.governed.fga import user_object; print(user_object(sys.argv[1]))" "$PRINCIPAL")"

# One call on a catalog door; success is any of the codes named, anything else prints the answer and fails.
door() {
  local token="$1" method="$2" path="$3" body="$4" out code; shift 4
  out="$(mktemp)"
  code="$(curl -s -o "$out" -w '%{http_code}' -m 60 -X "$method" "http://localhost:2333$path" \
    -H "authorization: Bearer $token" -H 'content-type: application/json' -d "$body" || true)"
  case " $* " in *" $code "*) rm -f "$out"; return 0 ;; esac
  echo "!! $method $path answered ${code:-no response}: $(head -c 400 "$out")" >&2
  rm -f "$out"
  return 1
}

# THE TENANT, BUILT THE WAY A USER BUILDS ONE: project, then warehouse, then namespaces, each through its door, so every
# grant the suites write with is the one the catalog writes, and no tuple is written by hand. Fixed ids rather than one
# per run: a re-run against this cluster converges, and 409 is that convergence.
PROJECT=acme
WAREHOUSE=e2e-acme
door "$ALICE" POST /management/v1/projects "{\"id\":\"$PROJECT\"}" 200 201 409 || exit 1
# NEWEST store of that name — the same selector service_kit.governed.fga.provision uses (max created_at), so a boot-race
# double-create (two catalog pods provisioning against a freshly-rolled OpenFGA) can never make the seeder
# and the serving catalog disagree about which store the grants live in (CI flake 2026-07-15: the CLI
# verified can_create_warehouse=allowed on stores[0] while the catalog checked its own newer store → 403).
# OpenFGA admits only a projected `rask-openfga` token ([[XC-077]]): the seeder speaks as the bootstrap Job's
# ServiceAccount, through a TokenRequest the suites re-read from LANCE_E2E_FGA_TOKEN_FILE.
FGA_TOKEN_FILE="$(mktemp)"
kubectl create token "$RELEASE-sa-jobs" --audience rask-openfga --duration 2h > "$FGA_TOKEN_FILE" || { echo "!! could not mint a rask-openfga token"; exit 1; }
FGA_TOKEN="$(cat "$FGA_TOKEN_FILE")"
SID="$(fga store list --api-url http://localhost:8081 --api-token "$FGA_TOKEN" \
  | uv run python -c "import sys,json;s=[x for x in json.load(sys.stdin)['stores'] if x['name']=='lance-catalog'];print(max(s,key=lambda x:x['created_at'])['id'])")"
# The project's creator holds `admin` on it, written by the create. bob gets NOTHING on it — he is the 403 leg.
# WAIT until that grant is READABLE before using it. OpenFGA on Postgres is eventually consistent: a write returns
# BEFORE a subsequent `check` is guaranteed to see it, so the warehouse create below can 403 on a fresh stack — a race
# that makes the job "green once, flaky forever". Poll the permission the next door checks.
for i in $(seq 1 30); do
  allowed="$(fga query check --api-url http://localhost:8081 --api-token "$FGA_TOKEN" --store-id "$SID" \
    "$ALICE_USER" can_create_warehouse "project:$PROJECT" 2>/dev/null \
    | uv run python -c "import sys,json;print(json.load(sys.stdin).get('allowed', False))" 2>/dev/null || echo False)"
  [ "$allowed" = "True" ] && { echo "   project:$PROJECT is alice's (can_create_warehouse=allowed, store ${SID:0:8}…)"; break; }
  [ "$i" = "30" ] && { echo "!! alice's admin on project:$PROJECT never became readable"; exit 1; }
  sleep 1
done
door "$ALICE" POST /management/v1/warehouses "{\"id\":\"$WAREHOUSE\",\"project\":\"$PROJECT\"}" 200 201 409 || exit 1
# The parent namespace the multibase suite creates under. A top-level namespace belongs to a
# warehouse, and its creator owns it, so alice's child creates and table creates under it are authorized.
door "$ALICE" POST "/management/v1/warehouses/$WAREHOUSE/namespaces" '{"namespace":"mbns","adopt_existing":true}' 200 201 409 || exit 1
echo "   project $PROJECT > warehouse $WAREHOUSE > namespace mbns"

# ---- a SECOND tenant, so the credential-isolation legs can run ----------------------------------
# Five legs in `test_credential_isolation_e2e.py` assert that a credential vended for tenant A is REFUSED on tenant
# B's data — the sharpest statement of storage isolation this estate makes. BOB ADMINISTERS B WHILE ALICE ADMINISTERS
# acme, which is what makes this a cross-tenant test rather than one principal talking to two of its own projects.
# Minting a project is the estate admin's, so alice creates it and grants bob through the project's members door.
TENANT_B=e2etenantb
door "$ALICE" POST /management/v1/projects "{\"id\":\"$TENANT_B\"}" 200 201 409 || exit 1
door "$ALICE" PUT "/management/v1/projects/$TENANT_B/members" "{\"user\":\"$BOB_PRINCIPAL\",\"relation\":\"admin\"}" 200 || exit 1
echo "   tenant B: $TENANT_B (admin=bob)"

step "8/9 the outbox suite's Dapr app token"
DAPR_TOKEN="$(kubectl get secret "$RELEASE-dapr-app-token" -o jsonpath='{.data.token}' | base64 -d)"
[ -n "$DAPR_TOKEN" ] || { echo "!! no dapr app token"; exit 1; }

step "9/9 run the guarded e2e suites against the live stack"
export LANCE_E2E_S3=http://localhost:9900
# The CAS suite reads LANCE_E2E_S3_ENDPOINT (a DIFFERENT name). It was never exported, so all 3 CAS tests
# skipped on every run — the suite named in the goal has, until now, never actually executed.
# The maintenance-on-real-S3 suite (#80) gates on the same variable. It is safe to run against the live
# store precisely because it never READS one: it creates four uuid-suffixed buckets of its own, sweeps
# only those, and deletes them — `run_sweep` compacts and GCs everything it discovers, so pointing it at
# `lance-catalog` would rewrite the estate this job just seeded.
export LANCE_E2E_S3_ENDPOINT=http://localhost:9900
export LANCE_E2E_S3_ACCESS_KEY="$S3_KEY"
export LANCE_E2E_S3_SECRET_KEY="$S3_SECRET"
export LANCE_E2E_CATALOG_URL=http://localhost:2333
export LANCE_E2E_LINEAGE_URL=http://localhost:18000
# WITH the /dex path prefix — that is what every suite defaults to, and what Dex actually serves the OIDC
# discovery document under. Exporting the bare host OVERRODE the correct default with a broken one, so the
# client-direct suite's reachability probe 404'd and it SKIPPED — silently, on every run.
export LANCE_E2E_DEX=http://localhost:5556/dex
export LANCE_E2E_FGA=http://localhost:8081
export LANCE_E2E_FGA_TOKEN_FILE="$FGA_TOKEN_FILE"
export LANCE_E2E_TOKEN="$ALICE"
export LANCE_E2E_NONADMIN_TOKEN="$BOB"
export LANCE_E2E_DAPR_TOKEN="$DAPR_TOKEN"
export LANCE_E2E_PROJECT="$PROJECT"
# The suites' top-level namespaces arrive through this warehouse (`topology.create_top_level`, `outbox_probe`).
export LANCE_E2E_WAREHOUSE="$WAREHOUSE"
export LANCE_E2E_PROJECT_B="$TENANT_B"
export LANCE_E2E_TENANT_B_TOKEN="$BOB"
export LANCE_E2E_DELIM='$'
export LANCE_E2E_BASE_A="$BASE_A"
export LANCE_E2E_BASE_B="$BASE_B"
export LANCE_E2E_OUTBOX_URI="s3://lance-catalog/_lineage_outbox"
export LANCE_E2E_CONTROL_OUTBOX_URI="s3://lance-catalog/_control_outbox"
export LANCE_E2E_RELAY_BINDING=lineage-relay-cron
# The bring-your-own scenario and criterion 4's module: the notifications service under the release's API prefix, the
# bus, and the release whose ServiceAccounts sign.
export LANCE_E2E_NOTIFICATIONS_URL="http://localhost:8850$(kubectl get cm "$RELEASE-config" -o jsonpath='{.data.RASK_API_PREFIX}')"
export LANCE_E2E_NATS_URL=nats://localhost:4222
export LANCE_E2E_RELEASE="$RELEASE"
# Criterion 2's service identity: the lineage service's account, for the audience the catalog's own door checks.
SERVICE_TOKEN_FILE="$(mktemp)"
SA_AUDIENCE="$(kubectl get deploy "$RELEASE-catalog" -o jsonpath='{.spec.template.spec.containers[?(@.name=="catalog")].env[?(@.name=="RASK_SA_AUDIENCE")].value}')"
kubectl create token "$RELEASE-sa-lineage" --audience "$SA_AUDIENCE" --duration 2h > "$SERVICE_TOKEN_FILE" || { echo "!! could not mint a $SA_AUDIENCE token for $RELEASE-sa-lineage"; exit 1; }
export LANCE_E2E_SERVICE_TOKEN_FILE="$SERVICE_TOKEN_FILE"

# CRITERION 5'S DRILLS RUN LAST, because each takes a dependency of this release down (XC-095): by the time they run,
# every other suite has read what it needed. Each restores what it took down in a `finally`, and the shared tenant is
# torn down at session end through the doors they leave serving. `E2E_CHAOS=0` leaves them out while iterating.
CHAOS=(tests/e2e-py/test_the_estate_is_resilient.py --chaos)
[ "${E2E_CHAOS:-1}" = "1" ] || CHAOS=()
PYTHONPATH=services uv run pytest \
  tests/e2e-py/test_credential_isolation_e2e.py \
  tests/e2e-py/test_object_store_cas_e2e.py \
  tests/e2e-py/test_warehouses_e2e.py \
  tests/e2e-py/test_multibase_e2e.py \
  tests/e2e-py/test_outbox_crash_e2e.py \
  tests/e2e-py/test_maintenance_s3_e2e.py \
  tests/e2e-py/test_track_a_acceptance.py \
  tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py \
  tests/e2e-py/test_provenance_is_correct.py \
  tests/e2e-py/test_events_are_correct.py \
  tests/e2e-py/test_the_catalog_is_correct.py \
  "${CHAOS[@]}" \
  -v -rs -p no:cacheprovider | tee /tmp/e2e-stack.log

# NO SILENT SKIPS. Every suite above is skip-guarded on "is the stack reachable?" — which is the right
# behaviour on a laptop with nothing running, and a LIE here: the stack IS up, so a skip means an env var is
# misnamed and the suite silently did not run. That is not hypothetical. It is how the CAS suite (3 tests,
# wrong var name) and the client-direct suite (2 tests, Dex URL missing its /dex prefix) went COMPLETELY
# UNEXECUTED for the entire life of this job while it reported "6 passed" and a green check.
# A green tick over a suite that never ran is worse than a red one.
if grep -qE "[1-9][0-9]* skipped" /tmp/e2e-stack.log; then
  echo
  echo "!! FAIL: e2e suites SKIPPED against a LIVE stack — that is a misconfiguration, not 'not applicable':"
  grep -E "^SKIPPED|skipped" /tmp/e2e-stack.log || true
  exit 1
fi

echo
echo "✓ e2e stack suite green — the live proof is now an artifact, not an anecdote"
