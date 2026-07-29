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
else
  echo "!! '${SERVICE}' is neither services/<name> nor frontend/microfrontends/<name>"
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

if [ "$KIND" = "zone" ]; then
  printf '<!-- %s -->\n' "$MARKER" >> "$SRC"
else
  printf '# %s\n' "$MARKER" >> "$SRC"
fi
echo ">> wrote marker into ${SRC}, watching ${SITE}"
START=$(date +%s)

while [ $(( $(date +%s) - START )) -lt "$TIMEOUT" ]; do
  if "$KUBECTL" exec "$POD" -c "$CONTAINER" -- grep -q "$MARKER" "$SITE" 2>/dev/null; then
    echo ">> SYNCED in $(( $(date +%s) - START ))s"
    case "$RELOAD" in
      yes*) ;;
      *) echo "!! synced, but the change is INERT: ${RELOAD}"; exit 1 ;;
    esac
    echo ">> live_update is working"
    exit 0
  fi
  sleep 1
done

echo "!! marker never reached the pod within ${TIMEOUT}s — live_update is NOT working"
[ "$KIND" = "zone" ] && echo "   for a zone the rebuild step also runs, so allow more than the ${TIMEOUT}s default: TIMEOUT=240"
echo "   check: is 'tilt up' running? does the Tiltfile sync this service? is the pod tilt's build?"
exit 1
