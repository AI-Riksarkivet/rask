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
#
# Exit 0 = the edit reached the pod (prints the elapsed seconds). Exit 1 = it did not.
set -euo pipefail

SERVICE="${SERVICE:-catalog}"
TIMEOUT="${TIMEOUT:-90}"
KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"; export KUBECONFIG
KUBECTL="${KUBECTL:-$(dirname "$0")/../.localbin/kubectl}"
[ -x "$KUBECTL" ] || KUBECTL=kubectl

SRC="services/${SERVICE}/src/${SERVICE}/__init__.py"
SITE="/opt/venv/lib/python3.13/site-packages/${SERVICE}/__init__.py"
[ -f "$SRC" ] || { echo "!! no such source file: $SRC"; exit 1; }

POD="$("$KUBECTL" get pods -l "app.kubernetes.io/component=${SERVICE}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$POD" ] || { echo "!! no running pod for component=${SERVICE}"; exit 1; }

# A pod whose uvicorn lacks --reload can still receive the file, so report that separately:
# the sync can succeed while the worker never re-reads it. Both must hold to call it working.
CMD="$("$KUBECTL" exec "$POD" -c "$SERVICE" -- sh -c 'cat /proc/1/cmdline | tr "\0" " "' 2>/dev/null || true)"
case "$CMD" in
  *--reload*) RELOAD="yes" ;;
  *)          RELOAD="NO — dev.reload is not set on this deploy; a synced file will NOT be re-read" ;;
esac

MARKER="TILT_VERIFY_$$_$(cksum <<<"$POD" | cut -d' ' -f1)"
echo ">> service=${SERVICE} pod=${POD}"
echo ">> uvicorn --reload: ${RELOAD}"

CLEAN() { sed -i "/^# ${MARKER}\$/d" "$SRC" 2>/dev/null || true; }
trap CLEAN EXIT INT TERM

printf '# %s\n' "$MARKER" >> "$SRC"
echo ">> wrote marker into ${SRC}, watching ${SITE}"
START=$(date +%s)

while [ $(( $(date +%s) - START )) -lt "$TIMEOUT" ]; do
  if "$KUBECTL" exec "$POD" -c "$SERVICE" -- grep -q "$MARKER" "$SITE" 2>/dev/null; then
    echo ">> SYNCED in $(( $(date +%s) - START ))s"
    [ "$RELOAD" = "yes" ] || { echo "!! synced, but the worker has no --reload, so the change is INERT"; exit 1; }
    echo ">> live_update is working"
    exit 0
  fi
  sleep 1
done

echo "!! marker never reached the pod within ${TIMEOUT}s — live_update is NOT working"
echo "   check: is 'tilt up' running? does the Tiltfile sync this service? is the pod tilt's build?"
exit 1
