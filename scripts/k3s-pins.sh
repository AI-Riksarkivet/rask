#!/usr/bin/env bash
# Write a values file pinning EVERY image to what the cluster is actually running (#135).
#
# The chart has one `image.tag`, and this fleet has never been one tag: a catalog tag across 11
# services, a different tag on gateway/compute/notifications, a third across the 7 zones, and
# a DIGEST on one more. Rendering all of them from one value is what made `helm upgrade` destructive —
# every image rewritten to a tag the node does not hold, whole fleet ImagePullBackOff, recovered by
# hand. Measured twice on 2026-08-06.
#
# So: read the truth out of the cluster and hand it to helm. `helm upgrade -f <this>` then changes
# only what you meant to change. It is a CAPTURE, not a source of truth — the fix for a stale pin is
# to rebuild that image and re-run this, never to hand-edit the file.
#
# `--check-only` runs the divergence guard below and writes nothing. [[LH-169]]: the guard was reachable
# only by asking for a pin file, so an upgrade from a split estate proceeded in silence and reverted
# whichever half of the stem had been rolled forward. `make k3s-up` runs this mode before helm, so the
# refusal now stands in front of the destructive operation rather than beside it.
set -euo pipefail

# THE INTERPRETER IS RESOLVED, NOT NAMED. This script parses the cluster's JSON in python, and a bare
# `python3` is a name rather than a guarantee: the Dagger test container that drives the divergence
# guard has uv's python and no `python3` on PATH, so line 47 died `python3: command not found` and the
# gate reported rc=127 — which its own assertion read as "a split stem was accepted". A gate that
# cannot RUN is worse than one that cannot fail, and `ms-test` was red on this for days while three
# e2e lanes declaring `needs: ms-test` executed not once.
PY_BIN="${PY_BIN:-}"
if [ -z "$PY_BIN" ]; then
  for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 && { PY_BIN="$candidate"; break; }
  done
fi
# `uv run python` last: it is the slowest of the three and the only one that can install, so it is the
# answer for an environment that has uv and no interpreter on PATH rather than the first thing tried.
[ -n "$PY_BIN" ] || { command -v uv >/dev/null 2>&1 && PY_BIN="uv run python"; }
[ -n "$PY_BIN" ] || { echo "!! k3s-pins needs a python interpreter (tried python3, python, uv run python)" >&2; exit 1; }

# THE CLUSTER THIS SCRIPT MEANS ([[XC-057]]). This one targets the deployed estate on purpose, and
# saying so is the point: an absent declaration and a deliberate one used to look identical, so
# "I meant the live cluster" was indistinguishable from "I never thought about it". Overridable, so a
# second estate (another host, another context) is a variable rather than an edit.
: "${RASK_EXPECT_CONTEXT:=default}"
export RASK_EXPECT_CONTEXT

KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
CHECK_ONLY=""
if [[ "${1:-}" == "--check-only" ]]; then CHECK_ONLY=1; shift; fi
OUT="${1:-deploy/values-live-pins.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
# The release's namespace, as the Makefile states it ([[XC-152]]): read from the context instead, a context in another
# namespace finds no workload and the stem check passes having checked nothing.
NS=(--namespace "${RELEASE_NAMESPACE:-default}")
export CHECK_ONLY
# The staging file is removed on ANY exit, so a refused run leaves neither a truncated $OUT (see the
# `mv` at the end) nor a half-written sibling for the next reader to mistake for output.
trap 'rm -f "$OUT.tmp"' EXIT

# THE RASK RELEASE'S WORKLOADS ONLY. A RayCluster is the engine's (ADR 0153; in development the ray-cluster fixture,
# whose image tag that fixture's own release carries), so its head is not pinned here, and a fixture's workload that
# shares this namespace (the NATS bus, [[XC-129]]) belongs to its own release.
KINDS="deploy,statefulset"
"$KUBECTL" "${NS[@]}" get "$KINDS" -o json | $PY_BIN -c '
import json, os, sys

tags, digests = {}, {}
# component -> {tag_or_digest: [workloads running it]}. The chart has ONE tag per image stem, so a stem
# running two different tags cannot be represented in the output at all — see the guard below.
sources = {}
def _pod_specs(item):
    """Every pod spec a workload owns — one for a Deployment/StatefulSet, several for a RayCluster.

    KubeRay nests its pods under `headGroupSpec` and `workerGroupSpecs` instead of the `spec.template`
    every other workload uses, so a reader that only knows the common shape sees a RayCluster as having
    no containers at all and skips it without saying so.
    """
    spec = item.get("spec", {})
    if "template" in spec:
        return [spec["template"]["spec"]]
    groups = [spec["headGroupSpec"]] if "headGroupSpec" in spec else []
    groups += spec.get("workerGroupSpecs", [])
    return [g["template"]["spec"] for g in groups if "template" in g]


for item in json.load(sys.stdin)["items"]:
    owner = item["metadata"]["name"]
    # A fixture workload in this namespace (the NATS bus) belongs to its own release, and so does its image tag.
    if (item["metadata"].get("annotations") or {}).get("meta.helm.sh/release-name", "rask") != "rask":
        continue
    for pod_spec in _pod_specs(item):
      for c in pod_spec.get("containers", []):
        ref = c["image"]
        # FIRST-PARTY ONLY. `image.tags` feeds `rask.image`, which renders only the images this repo
        # builds; dex, openfga, openbao and friends come from their own values and pinning
        # them here would be noise that looks authoritative. Ours is either bare (side-loaded) or under
        # the local registry (localhost / 172.*). ANY other path head is third-party, including Docker
        # Hub short forms with no dot: `rustfs/rustfs:1.0.3` would be recorded as component `rustfs`, a pin
        # that renders a side-loaded `rustfs:1.0.3` no registry holds.
        head = ref.split("/", 1)[0] if "/" in ref else ""
        if head and not head.startswith("localhost") and not head.startswith("172."):
            continue
        # Strip the registry: the chart supplies it from image.repository, and a pin that carried it
        # would fight that value instead of complementing it.
        name = ref.rsplit("/", 1)[-1]
        if "@" in name:
            component, digest = name.split("@", 1)
            digests[component] = digest
            sources.setdefault(component, {}).setdefault("@" + digest, []).append(owner)
        elif ":" in name:
            component, tag = name.rsplit(":", 1)
            tags[component] = tag
            sources.setdefault(component, {}).setdefault(tag, []).append(owner)

# REFUSE rather than guess. `tags[component] = tag` is keyed by image STEM, and eleven workloads share
# the `lance-rest-catalog` stem here. When one of them runs a different tag — the normal result of a
# `kubectl set image` on a single deployment — the assignment above is LAST-WRITER-WINS by dict
# iteration order, and the losing tag vanishes from a file whose whole promise is "what the cluster is
# actually RUNNING". The next `make k3s-up` then reverts that workload with nothing reporting it.
# MEASURED 2026-08-16: rask-maintenance ran `maint-0517356a` (two shipped fixes) while its ten stem-mates
# ran `main-d7d75cf0`; the generated file recorded only the latter, so an upgrade would have silently
# rolled the fixes back. There is no honest pin to emit in that state, so this exits instead — the
# resolution is to build ONE image carrying every change and roll the whole stem, not to hand-edit.
split = {c: v for c, v in sorted(sources.items()) if len(v) > 1}
if split:
    sys.stderr.write("ERROR: these image stems run MORE THAN ONE tag; the chart has one tag per stem,\n")
    sys.stderr.write("so no correct pin file exists until they converge:\n\n")
    for component, by_tag in split.items():
        sys.stderr.write("  " + component + "\n")
        for tag, owners in sorted(by_tag.items()):
            sys.stderr.write("    " + tag + "  <- " + ", ".join(sorted(owners)) + "\n")
    sys.stderr.write("\nRebuild one image from a commit carrying every change, roll the whole stem, re-run.\n")
    sys.exit(1)

if os.environ.get("CHECK_ONLY"):
    # The guard is the whole point of this mode; emitting the pin body as well would hand `k3s-up` a
    # values file it never asked for.
    sys.stderr.write("stems converged: " + str(len(sources)) + " first-party images, one tag each\n")
    sys.exit(0)

def block(label, mapping):
    if not mapping:
        return f"  {label}: {{}}\n"
    lines = "".join(f"    {k}: {json.dumps(v)}\n" for k, v in sorted(mapping.items()))
    return f"  {label}:\n{lines}"

sys.stdout.write(
    "# GENERATED by scripts/k3s-pins.sh — what the cluster is RUNNING, not what anyone intended.\n"
    "# Regenerate after every build+roll; never hand-edit. See #135.\n"
    "image:\n" + block("tags", tags) + block("digests", digests)
)
' > "$OUT.tmp"

# ATOMIC, and the divergence guard is exactly why. `> "$OUT"` truncates the file the instant the shell
# builds the redirection — BEFORE python runs — so any non-zero exit left the previous, good pins
# destroyed. The guard added above exits 1 by design on a divergent stem, which turned a deliberate
# refusal into "your record of what the cluster runs is now an empty file". Committed and pushed once
# (2026-08-16) before anyone noticed; an upgrade reading a 0-byte pin file supplies no image tags at
# all. Write to a sibling and move only on success, so a refusal costs nothing.
if [[ -n "$CHECK_ONLY" ]]; then exit 0; fi
mv "$OUT.tmp" "$OUT"

echo ">> wrote $OUT"
grep -c ':' "$OUT" || true

# THE ENGINE FIXTURE'S HEAD, pinned in its own file: the rask release renders no RayCluster (ADR 0153), so its image
# tag belongs to the ray-cluster fixture's release, which scripts/fixtures.sh layers this file onto. An estate whose
# engine is not the fixture has no `<release>-ray` RayCluster and gets no file.
RAY_PINS="deploy/fixtures/ray-cluster/values-live-pins.yaml"
RAY_IMAGE="$("$KUBECTL" "${NS[@]}" get raycluster rask-ray -o jsonpath='{.spec.headGroupSpec.template.spec.containers[0].image}' 2>/dev/null || true)"
if [[ -n "$RAY_IMAGE" && -z "$CHECK_ONLY" ]]; then
  printf '# GENERATED by scripts/k3s-pins.sh — the image the ray-cluster fixture'"'"'s head is RUNNING. Never hand-edit.\nimage:\n  repository: "%s"\n  tag: "%s"\n' \
    "${RAY_IMAGE%:*}" "${RAY_IMAGE##*:}" > "$RAY_PINS.tmp" && mv "$RAY_PINS.tmp" "$RAY_PINS"
  echo ">> wrote $RAY_PINS"
fi

# THE COMPUTE FIXTURE'S IMAGE, likewise: compute is its own release (deploy/fixtures/compute), so its tag belongs to that
# release, which scripts/fixtures.sh layers this file onto. An estate that runs no `<release>-compute` gets no file.
COMPUTE_PINS="deploy/fixtures/compute/values-live-pins.yaml"
COMPUTE_IMAGE="$("$KUBECTL" "${NS[@]}" get deployment rask-compute -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
if [[ -n "$COMPUTE_IMAGE" && -z "$CHECK_ONLY" ]]; then
  printf '# GENERATED by scripts/k3s-pins.sh — the image the compute fixture is RUNNING. Never hand-edit.\nimage:\n  repository: "%s"\n  tag: "%s"\n' \
    "${COMPUTE_IMAGE%:*}" "${COMPUTE_IMAGE##*:}" > "$COMPUTE_PINS.tmp" && mv "$COMPUTE_PINS.tmp" "$COMPUTE_PINS"
  echo ">> wrote $COMPUTE_PINS"
fi

# THE TABLE-MAINTENANCE FIXTURE'S IMAGE, likewise (deploy/fixtures/table-maintenance, ADR 0174): the image its CronJob
# starts each run with. An estate that runs no `<release>-table-maintenance` gets no file.
MAINTENANCE_JOB_PINS="deploy/fixtures/table-maintenance/values-live-pins.yaml"
MAINTENANCE_JOB_IMAGE="$("$KUBECTL" "${NS[@]}" get cronjob rask-table-maintenance -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
if [[ -n "$MAINTENANCE_JOB_IMAGE" && -z "$CHECK_ONLY" ]]; then
  printf '# GENERATED by scripts/k3s-pins.sh — the image the table-maintenance fixture runs. Never hand-edit.\nimage:\n  repository: "%s"\n  tag: "%s"\n' \
    "${MAINTENANCE_JOB_IMAGE%:*}" "${MAINTENANCE_JOB_IMAGE##*:}" > "$MAINTENANCE_JOB_PINS.tmp" && mv "$MAINTENANCE_JOB_PINS.tmp" "$MAINTENANCE_JOB_PINS"
  echo ">> wrote $MAINTENANCE_JOB_PINS"
fi
