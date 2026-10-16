#!/usr/bin/env bash
# Build ONE image with Dagger and put it where the caller needs it.
#
# (`k3s-build`, `frontend-images`), the e2e stack scripts, and CI (which calls those make targets).
# Before 2026-07-29 each of those shelled out to `docker buildx build` directly, so "dagger is the build
# system" was true of the CI gates and false of every artefact. The dockerfile is unchanged and remains
# the single source of truth; only the driver moved.
#
#   scripts/dagger-image.sh --name notifications --tag notifications:dev
#   scripts/dagger-image.sh --name rest-catalog --tag lance-rest-catalog:dev
#   scripts/dagger-image.sh --zone lakehouse    --tag web-lakehouse:dev
#   scripts/dagger-image.sh --runner htr        --tag ray-htr:dev
#   scripts/dagger-image.sh --name cnpg-age-ext --tag cnpg-age:dev --arg AGE_REF=v1.5.0
#   scripts/dagger-image.sh --name notifications --out build/k3s-images/notifications.tar   (no docker: what k3s-build does)
#
# DELIVERY: --load (default) exports an OCI tarball and `docker load`s it, which reproduces exactly what
# `docker buildx build --load` used to do, so the compose stacks and the kind lane keep working unchanged.
# --out stops at the tarball, which `make k3s-import` loads into k3s's containerd with no docker daemon. Measured on the gateway image: the export dominates at ~78 s (it is the BUILD), and the
# `docker load` adds ~5 s. --push publishes straight to a registry instead, skipping the daemon.
#
# THE ENGINE, AND WHAT EACH MODE ACTUALLY NEEDS OF IT. Dagger always speaks HTTPS to a registry and
# `publish` has no --insecure flag, so pushing to the plain-HTTP dev registry needs the CONFIG that
# `make dagger-engine` writes. --load touches no registry and needs none of that — what it needs is to
# not split the BuildKit CACHE, because an engine owns its own: letting --load auto-provision a second
# engine means a build warms one and the next reads the other cold, buying nothing. Measured
# 2026-09-20 on this host — two engines up at once, `dagger-engine-rask` holding 5.2 GB against an
# auto-provisioned `dagger-engine-v0.21.7` on its own anonymous volume, invisible because a cold build
# looks exactly like a slow one.
#
# THOSE ARE TWO DIFFERENT REQUIREMENTS AND ONLY ONE IS UNIVERSAL. A cache can only be split where a
# cache persists. CI is a fresh runner with no engine, no state volume and no `make dagger-engine`
# step, so demanding the repo's engine there refuses a build to protect a cache that does not exist —
# which is how `e2e-stack` and `e2e-ray` died on `!! dagger-engine-rask is not running` the first time
# they reached this script. So the requirement is stated by its reason: --push always needs the
# engine's config; --load needs it only when the NAMED state volume is there to be split, i.e. on a
# host where `make dagger-engine` has run and the engine is merely stopped.
# `make dagger-engine` is idempotent and once per host; it reuses that volume, so running it costs
# nothing and never discards the cache.
set -euo pipefail

NAME="" ZONE="" RUNNER="" TAG="" MODE="load" ADDRESS="" OUT=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)    NAME="$2"; shift 2 ;;
    --zone)    ZONE="$2"; shift 2 ;;
    --runner)  RUNNER="$2"; shift 2 ;;
    --tag)     TAG="$2"; shift 2 ;;
    --arg)     EXTRA_ARGS+=(--build-arg "$2"); shift 2 ;;
    --push)    MODE="push"; ADDRESS="$2"; shift 2 ;;
    --out)     MODE="out"; OUT="$2"; shift 2 ;;
    *) echo "!! unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$NAME" && -z "$ZONE" && -z "$RUNNER" ]]; then
  echo "!! need --name <dockerfile stem>, --zone <zone>, or --runner <runner>" >&2; exit 2
fi
if [[ "$MODE" == "load" && -z "$TAG" ]]; then
  echo "!! --load needs --tag <image:tag>" >&2; exit 2
fi

command -v dagger >/dev/null 2>&1 || { echo "!! dagger CLI not on PATH — https://docs.dagger.io/install" >&2; exit 1; }

# Respect an operator's choice; otherwise prefer the repo's configured engine when it is up. --out is held to --load's
# rule: it writes a file instead of loading into docker, but it builds on the same engine and must not split its cache.
if [[ -z "${_EXPERIMENTAL_DAGGER_RUNNER_HOST:-}" ]]; then
  engine="${DAGGER_ENGINE_NAME:-dagger-engine-rask}"
  state="${DAGGER_ENGINE_STATE:-dagger-engine-rask-state}"
  if [[ "$(docker inspect -f '{{.State.Running}}' "$engine" 2>/dev/null || echo false)" == "true" ]]; then
    export _EXPERIMENTAL_DAGGER_RUNNER_HOST="docker-container://$engine"
  elif [[ "$MODE" == "push" ]]; then
    echo "!! --push needs $engine's insecure-registry config and it is not running: run 'make dagger-engine'" >&2
    exit 1
  elif docker volume inspect "$state" >/dev/null 2>&1; then
    echo "!! $engine is not running but its cache volume $state is right here: run 'make dagger-engine'" >&2
    echo "!! (auto-provisioning a second engine would build cold against a warm cache nothing would read)" >&2
    exit 1
  else
    echo ">> no $engine and no $state volume — nothing to split, letting dagger provision its own engine" >&2
  fi
fi

# OCI provenance. Supplied HERE rather than computed inside the Dagger Function: a module is sandboxed
# and cannot read git, and stamping a timestamp inside it would change the build args on every single
# invocation and defeat the layer cache.
BUILD_DATE="${BUILD_DATE:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
VCS_REF="${VCS_REF:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
VERSION="${VERSION:-${TAG##*:}}"

if [[ -n "$ZONE" ]]; then
  fn=(zone-image --zone="$ZONE")
elif [[ -n "$RUNNER" ]]; then
  # One workload's Ray image, from the parametrized .docker/ray-runner.dockerfile.
  fn=(runner-image --runner="$RUNNER")
else
  fn=(image --name="$NAME")
fi
fn+=(--build-date="$BUILD_DATE" --vcs-ref="$VCS_REF" --version="$VERSION")
[[ ${#EXTRA_ARGS[@]} -gt 0 ]] && fn+=("${EXTRA_ARGS[@]}")

if [[ "$MODE" == "push" ]]; then
  echo ">> dagger: ${ZONE:-$NAME} -> $ADDRESS"
  exec dagger call "${fn[@]}" publish --address="$ADDRESS"
fi

# --out: the OCI tarball at the caller's path and nothing else; no image is loaded into docker ([[XC-152]]).
# `make k3s-import` loads it into k3s's containerd under the name the chart expects.
if [[ "$MODE" == "out" ]]; then
  mkdir -p "$(dirname "$OUT")"
  echo ">> dagger: ${ZONE:-${RUNNER:-$NAME}} -> $OUT"
  dagger call "${fn[@]}" export --path="$OUT"
  # NAMED WHEN A TAG IS GIVEN. Dagger's export carries no image name, and containerd names an image it imports from an
  # OCI layout by the `io.containerd.image.name` annotation on the index's manifest entry, a full reference; the layout's
  # `manifest.json` is not read at all (containerd v1.7 `images/archive/importer.go`, `import.go::imageName`). So the
  # annotation is written there, and `kind load image-archive` or a bare `ctr images import` lands the image under the
  # name the render asks for.
  if [[ -n "$TAG" ]]; then
    python3 - "$OUT" "$TAG" <<'PYEOF'
import io, json, os, sys, tarfile

path, tag = sys.argv[1], sys.argv[2]
first, _, rest = tag.partition("/")
if not rest:
    reference = f"docker.io/library/{tag}"
elif "." in first or ":" in first or first == "localhost":
    reference = tag
else:
    reference = f"docker.io/{tag}"
named = path + ".named"
with tarfile.open(path) as src, tarfile.open(named, "w") as dst:
    for member in src.getmembers():
        if member.name != "index.json":
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
            continue
        index = json.load(src.extractfile(member))
        for entry in index["manifests"]:
            entry.setdefault("annotations", {}).update(
                {"io.containerd.image.name": reference, "org.opencontainers.image.ref.name": reference.rsplit(":", 1)[1]}
            )
        body = json.dumps(index).encode()
        member.size = len(body)
        dst.addfile(member, io.BytesIO(body))
os.replace(named, path)
PYEOF
  fi
  exit 0
fi

tar="$(mktemp -d)/image.tar"
# shellcheck disable=SC2064  # expand $tar now, at trap-set time — the temp dir is per-invocation
trap "rm -rf '$(dirname "$tar")'" EXIT

echo ">> dagger: ${ZONE:-$NAME} -> $TAG"
dagger call "${fn[@]}" export --path="$tar"

# `docker load` prints the image ID, not the tag — the tarball carries no repo:tag, so tag it here.
loaded="$(docker load -i "$tar" | sed -n 's/^Loaded image ID: //p; s/^Loaded image: //p' | tail -1)"
[[ -n "$loaded" ]] || { echo "!! docker load produced no image" >&2; exit 1; }
docker tag "$loaded" "$TAG"
echo ">> loaded $TAG"
