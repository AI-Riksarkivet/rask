#!/usr/bin/env bash
# Does Tilt's live_update ACTUALLY reach a running pod?
#
# "tilt up started" and "the pod is Running" prove nothing about hot reload — this estate
# shipped a Tiltfile for months whose live_update synced into a path that did not exist,
# against services whose uvicorn had no --reload, and nothing ever reported a problem.
# This makes the claim falsifiable: write a unique marker into a real source file, poll the
# container's filesystem for it, and time the round trip.
#
#   ./scripts/tilt-verify.sh            # defaults to the catalog service
#   SERVICE=lineage ./scripts/tilt-verify.sh
#   SERVICE=home    ./scripts/tilt-verify.sh   # a micro-frontend ZONE
#
# Zones are verified too, because they are the half most likely to be believed without proof:
# they came into the Tilt loop on 2026-07-29 and their sync mechanism is different (sync + rebuild
# + restart, not uvicorn --reload). A zone also has a different precondition — its rootfs must be
# WRITABLE, which `frontends.yaml` did not honour until the same day, so every zone sync would have
# failed with "Read-only file system" while looking configured.
#
# Exit 0 = the edit reached the pod (prints the elapsed seconds). Exit 1 = it did not.
set -euo pipefail

SERVICE="${SERVICE:-catalog}"
TIMEOUT="${TIMEOUT:-90}"
KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"; export KUBECONFIG
KUBECTL="${KUBECTL:-$(dirname "$0")/../.localbin/kubectl}"
[ -x "$KUBECTL" ] || KUBECTL=kubectl

# Two target shapes. A Python service installs as a wheel into /opt/venv; a zone keeps its source
# at /app/app/src and is rebuilt in place. The component label and the CONTAINER name differ too —
# a zone's deployment is `rask-web-home` but its container is plain `home`, so `-c` needs the bare
# name while the label selector needs the prefixed one.
if [ -d "services/${SERVICE}" ]; then
  KIND="service"
  SRC="services/${SERVICE}/src/${SERVICE}/__init__.py"
  SITE="/opt/venv/lib/python3.13/site-packages/${SERVICE}/__init__.py"
  COMPONENT="${SERVICE}"
  CONTAINER="${SERVICE}"
  COMMENT="# "
elif [ -d "frontend/microfrontends/${SERVICE}" ]; then
  KIND="zone"
  SRC="frontend/microfrontends/${SERVICE}/src/app.html"
  SITE="/app/app/src/app.html"
  COMPONENT="web-${SERVICE}"
  CONTAINER="${SERVICE}"
  COMMENT="<!-- "   # app.html is HTML; a bare `# marker` line would render as page text
elif [ -d "frontend/packages/${SERVICE}" ]; then
  # The shared-library path, and the one most able to look fine while being broken. @rask/ui is the
  # ONLY package with a build step (svelte-package -> dist/) and every zone bundles that dist, never
  # its source — so a sync of packages/ that does not rebuild the library changes nothing a zone can
  # see, while reporting success. Verified against a ZONE's pod, because that is where the effect has
  # to land; the package has no pod of its own.
  KIND="package"
  ZONE="${ZONE:-home}"
  SRC="frontend/packages/${SERVICE}/src/lib/index.ts"
  [ -f "$SRC" ] || SRC="$(ls frontend/packages/${SERVICE}/src/lib/*.ts 2>/dev/null | head -1)"
  SITE="/app/packages/${SERVICE}/dist"
  COMPONENT="web-${ZONE}"
  CONTAINER="${ZONE}"
  COMMENT="// "
else
  echo "!! '${SERVICE}' is not services/<name>, frontend/microfrontends/<name> or frontend/packages/<name>"
  exit 1
fi
[ -f "$SRC" ] || { echo "!! no such source file: $SRC"; exit 1; }

POD="$("$KUBECTL" get pods -l "app.kubernetes.io/component=${COMPONENT}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$POD" ] || { echo "!! no running pod for component=${COMPONENT}"; exit 1; }

# A pod whose uvicorn lacks --reload can still receive the file, so report that separately:
# the sync can succeed while the worker never re-reads it. Both must hold to call it working.
if [ "$KIND" = "service" ]; then
  CMD="$("$KUBECTL" exec "$POD" -c "$CONTAINER" -- sh -c 'cat /proc/1/cmdline | tr "\0" " "' 2>/dev/null || true)"
  case "$CMD" in
    *--reload*) RELOAD="yes" ;;
    *)          RELOAD="NO — dev.reload is not set on this deploy; a synced file will NOT be re-read" ;;
  esac
else
  # A zone has no --reload: its live_update is sync + `bun run build` + restart. The equivalent
  # precondition is a WRITABLE rootfs — Tilt cannot even land the file otherwise, and that failure
  # is silent. Asked of the API rather than by attempting a write, so this stays read-only.
  RO="$("$KUBECTL" get pod "$POD" -o jsonpath="{.spec.containers[?(@.name==\"${CONTAINER}\")].securityContext.readOnlyRootFilesystem}" 2>/dev/null || true)"
  case "$RO" in
    true) RELOAD="NO — readOnlyRootFilesystem=true, so the sync cannot write into this pod at all" ;;
    *)    RELOAD="yes (zone: writable rootfs; reload is sync + rebuild + restart)" ;;
  esac
fi

# Is this pod even Tilt's? live_update only touches containers Tilt itself built and deployed.
# Running `helm upgrade` by hand while Tilt is up replaces Tilt's injected image with the chart
# default and Tilt silently stops managing that deployment — after which live_update CANNOT
# fire, no matter how correct everything else is. Without this check the failure is
# indistinguishable from a broken sync, which cost a long detour on 2026-07-28.
IMAGE="$("$KUBECTL" get pods "$POD" -o jsonpath='{.spec.containers[0].image}' 2>/dev/null || true)"
case "$IMAGE" in
  *:tilt-*) OWNED="yes" ;;
  *)        OWNED="NO" ;;
esac

# The pod BEFORE the edit. A rebuild + rollout also puts the marker in "a" pod, so without this the
# verifier passes on exactly the outcome live_update exists to avoid — and it did: the zone loop was
# answered by a full rebuild for months while every check here went green.
POD_BEFORE="$POD"

MARKER="TILT_VERIFY_$$_$(cksum <<<"$POD" | cut -d' ' -f1)"
echo ">> ${KIND}=${SERVICE} pod=${POD} container=${CONTAINER}"
echo ">> image=${IMAGE}"
echo ">> uvicorn --reload: ${RELOAD}"
if [ "$OWNED" = "NO" ]; then
  echo "!! this pod is NOT Tilt's build (Tilt tags images :tilt-<hash> and pushes to the"
  echo "   registry from make tilt-registry). Tilt is not managing this deployment, so"
  echo "   live_update cannot reach it. Someone ran 'helm upgrade' while Tilt was up, or"
  echo "   Tilt is not running. Restart Tilt and let it own the release:"
  echo "     sudo pkill -x tilt   # if a Tilt from another user holds :10350"
  echo "     make tilt-up"
  exit 1
fi

# Remove the marker line whatever happens — a verifier that leaves debris in tracked source is a
# verifier people stop running.
CLEAN() { sed -i "/${MARKER}/d" "$SRC" 2>/dev/null || true; }
trap CLEAN EXIT INT TERM

case "$KIND" in
  zone)    printf '<!-- %s -->\n' "$MARKER" >> "$SRC" ;;
  # No leading newline: CLEAN deletes the MARKER LINE, so a blank line prepended here survives it
  # and leaves the tracked file dirty — which is how a verifier stops being run.
  package) printf 'export const _%s = true;\n' "$MARKER" >> "$SRC" ;;
  *)       printf '# %s\n' "$MARKER" >> "$SRC" ;;
esac
echo ">> wrote marker into ${SRC}, watching ${SITE}"
START=$(date +%s)

# What counts as ARRIVED differs by kind, and for a zone the weaker check is the dangerous one.
#   service — the wheel in site-packages; uvicorn --reload re-reads it.
#   zone    — NOT src/. The Bun server serves the COMPILED build/, so a marker sitting in
#             /app/app/src while `bun run build` failed is an edit the user cannot see. That exact
#             state persisted for months (exit 137, the build OOM/kill) and a src-only check called
#             it working.
#   package — the library's dist/, which only svelte-package can produce.
arrived() {
  case "$KIND" in
    zone)    "$KUBECTL" exec "$1" -c "$CONTAINER" -- grep -rq "$MARKER" /app/app/build 2>/dev/null ;;
    package) "$KUBECTL" exec "$1" -c "$CONTAINER" -- grep -rq "$MARKER" "$SITE" 2>/dev/null ;;
    *)       "$KUBECTL" exec "$1" -c "$CONTAINER" -- grep -q "$MARKER" "$SITE" 2>/dev/null ;;
  esac
}

while [ $(( $(date +%s) - START )) -lt "$TIMEOUT" ]; do
  # Re-resolve the pod every poll: if Tilt fell back to a rebuild the old pod is gone, and execing
  # into a terminated pod would just time out and report the wrong cause.
  POD_NOW="$("$KUBECTL" get pods -l "app.kubernetes.io/component=${COMPONENT}" \
              --field-selector=status.phase=Running \
              -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -n "$POD_NOW" ] && arrived "$POD_NOW"; then
    ELAPSED=$(( $(date +%s) - START ))
    echo ">> ARRIVED in ${ELAPSED}s (${KIND}: $( [ "$KIND" = "service" ] && echo "$SITE" || echo "compiled output" ))"
    case "$RELOAD" in
      yes*) ;;
      *) echo "!! synced, but the change is INERT: ${RELOAD}"; exit 1 ;;
    esac
    # THE claim. A rebuild + rollout delivers the change too, in minutes, via a new ReplicaSet — that
    # is Tilt's FALLBACK, not live_update, and telling them apart is the entire point of this script.
    if [ "$POD_NOW" != "$POD_BEFORE" ]; then
      echo "!! but the POD CHANGED: ${POD_BEFORE} -> ${POD_NOW}"
      echo "   that is a full image rebuild + rollout, NOT live_update. Tilt falls back to this"
      echo "   whenever a changed file matches no sync rule (check: tilt get liveupdates -o json)"
      echo "   or the in-container run step fails (check: tilt get uiresources <r> -o json ->"
      echo "   buildHistory[].error, span liveupdate:*)."
      exit 1
    fi
    echo ">> SAME POD — no rebuild, no rollout"
    echo ">> live_update is working"
    exit 0
  fi
  sleep 1
done

echo "!! marker never reached the pod within ${TIMEOUT}s — live_update is NOT working"
[ "$KIND" = "zone" ] && echo "   for a zone the rebuild step also runs, so allow more than the ${TIMEOUT}s default: TIMEOUT=240"
echo "   check: is 'tilt up' running? does the Tiltfile sync this service? is the pod tilt's build?"
exit 1
