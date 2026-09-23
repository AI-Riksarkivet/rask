#!/usr/bin/env bash
# The image arguments `make k3s-up` hands to `helm upgrade`, one per line on stdout; what it decided
# and why goes to stderr.
#
#   IMGARGS=$(./scripts/k3s-image-args.sh <live-values-out> [pins-file])
#
# A SCRIPT, NOT RECIPE PROSE, because the decision has branches that must be driven by a test and the
# rationale is longer than a recipe can carry: make runs each LOGICAL recipe line in its own shell, and
# a comment line inside a backslash continuation ends that line — which once put the upgrade in a
# different shell from the `.env` loader (tests/unit/test_the_deploy_hands_helm_the_credentials_it_loads.py).
#
# THE PINS FILE CARRIES TAGS AND NOTHING ELSE, so it LAYERS over the registry rather than replacing it.
# `k3s-pins.sh` strips the registry on purpose ("the chart supplies it from image.repository"), so
# selecting the pins file INSTEAD of the live values leaves `image.repository` unset and the chart
# refuses the render outright — "must be set to a registry ... or set image.localImages=true".
# Measured 2026-09-09: every `make k3s-up` failed for as long as a non-empty pins file existed, which is
# the state a pin is FOR. Base first (the live release's image settings, else side-loaded), pins second,
# so the tags win and the registry survives.
#
# A PINS FILE DESCRIBES ONE CLUSTER, and it is tracked in git, so it reaches every other checkout.
# Applied anywhere else it names images that host never built — measured 2026-09-23 on a fresh host,
# where it pinned all fifteen first-party images to the previous host's tags while `make k3s-build` had
# produced `:dev`. `k3s-pins.sh` records the `kube-system` namespace UID it read from (`# cluster: <uid>`)
# and the file is layered only onto that same cluster. Unstamped, or unreadable here, means unproven,
# and an unproven capture is not applied (tests/unit/test_a_pins_file_only_describes_the_cluster_it_was_read_from.py).
#
# TWO ESTATES, TOLD APART BY ONE VALUE. A REGISTRY estate (`image.repository` set in the live release)
# keeps its own image settings: the live values are layered back (`-f`), then this cluster's pins. A
# SIDE-LOADED estate — no release yet, or one whose images are bare names the node holds — gets what
# `make k3s-build` made: every first-party image bare (`image.localImages`) at `$K3S_TAG`, the one tag
# the build stamped and the import loaded, zones included (the chart defaults them to an immutable
# commit tag a local build never produces). Pins are never layered there: they would beat `image.tag`
# and hold a rebuilt fleet on the tag it was captured at. The test is `.image.repository` itself, read
# as JSON — a substring match on `repository:` also matched the retired `ray.image.repository` and read
# a side-loaded release as a registry one (tests/unit/test_the_deploy_reads_the_release_it_upgrades.py).
#
# THE RETIRED RAY KEYS ARE MIGRATED, NOT DROPPED. `ray.image.repository` / `ray.image.tag` were the
# registry estate's only record of which head image it deploys; the head now renders through
# `rask.image`, so they become `ray.image.name` + `image.tags.<name>` in the same registry (the same
# reference), and are nulled out of the release. One naming another registry is refused: no pin in
# this estate's registry can express it. The chart refuses them on every other path
# (templates/ray-image-guard.yaml).
#
# `<live-values-out>` receives the live release's values and must outlive this script: helm reads it
# after we return. JSON is YAML, so helm reads the same file with `-f`.
set -euo pipefail

: "${K3S_TAG:?K3S_TAG must name the tag make k3s-build stamped (the Makefile passes it)}"
LIVE_OUT="${1:?usage: k3s-image-args.sh <live-values-out> [pins-file]}"
PINS="${2:-chart/values-live-pins.yaml}"
HELM="${HELM:-$(dirname "$0")/helm.sh}"
KUBECTL="${KUBECTL:-kubectl}"
UUID='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

registry="" legacy_repo="" legacy_tag=""
if "$HELM" get values rask -o json >"$LIVE_OUT" 2>/dev/null; then
  # One field per line, empty when absent. Captured into a variable so a failed parse ends the script
  # (`set -e`) instead of reading as "no registry" and upgrading a registry estate as a side-loaded one.
  fields="$(python3 -c '
import json, sys
values = json.load(open(sys.argv[1])) or {}
ray_image = (values.get("ray") or {}).get("image") or {}
for field in ((values.get("image") or {}).get("repository"), ray_image.get("repository"), ray_image.get("tag")):
    print(field or "")
' "$LIVE_OUT")"
  registry="$(sed -n 1p <<<"$fields")"
  legacy_repo="$(sed -n 2p <<<"$fields")"
  legacy_tag="$(sed -n 3p <<<"$fields")"
fi

if [[ -z "$registry" ]]; then
  args=(--set image.localImages=true --set-string "image.tag=$K3S_TAG" --set-string "frontend.image.tag=$K3S_TAG")
  echo ">> side-loaded estate: every first-party image as the bare <stem>:$K3S_TAG make k3s-build made" >&2
  if [[ -s "$PINS" ]]; then
    echo ">> NOT applying $PINS: a side-loaded estate runs what the local build made, at K3S_TAG" >&2
  fi
  printf '%s\n' "${args[@]}"
  exit 0
fi

args=(-f "$LIVE_OUT")
echo ">> registry estate ($registry): reusing the LIVE release's image settings — not imposing localImages" >&2

if [[ -n "$legacy_repo$legacy_tag" ]]; then
  name="${legacy_repo##*/}"
  if [[ "$legacy_repo" != "$registry/$name" || -z "$name" || -z "$legacy_tag" ]]; then
    echo "!! the live release carries the retired ray.image.repository='$legacy_repo' ray.image.tag='$legacy_tag', which is not an image in its registry ($registry)." >&2
    echo "   Set ray.image.name and image.tags.<name> for the Ray head explicitly, then re-run." >&2
    exit 1
  fi
  args+=(--set ray.image.repository=null --set ray.image.tag=null --set-string "ray.image.name=$name" --set-string "image.tags.$name=$legacy_tag")
  echo ">> migrating the retired ray.image.repository/tag ($legacy_repo:$legacy_tag) to ray.image.name=$name + image.tags.$name" >&2
fi

if [[ -s "$PINS" ]]; then
  captured="$(sed -n 's/^# cluster: *//p' "$PINS" | head -n 1)"
  here="$("$KUBECTL" get namespace kube-system -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
  if [[ -z "$captured" ]]; then
    echo ">> NOT applying $PINS: it does not record which cluster it describes — run 'make k3s-pins' on the cluster it should pin" >&2
  elif [[ ! "$here" =~ $UUID ]]; then
    echo ">> NOT applying $PINS: this cluster's identity (kube-system uid) could not be read, so no capture can be shown to match it" >&2
  elif [[ "$captured" != "$here" ]]; then
    echo ">> NOT applying $PINS: it was captured on cluster $captured and this is cluster $here — run 'make k3s-pins' here once a release is running" >&2
  else
    args+=(-f "$PINS")
    echo ">> image TAGS pinned from $PINS, captured on this cluster ($here)" >&2
  fi
fi
printf '%s\n' "${args[@]}"
