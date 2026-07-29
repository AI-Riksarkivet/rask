#!/usr/bin/env bash
# Build ONE image with Dagger and put it where the caller needs it.
#
# This is the single seam through which every non-Tilt image build in this repo goes — the Makefile
# (`k3s-build`, `frontend-images`), the e2e stack scripts, and CI (which calls those make targets).
# Before 2026-07-29 each of those shelled out to `docker buildx build` directly, so "dagger is the build
# system" was true of the CI gates and false of every artefact. The dockerfile is unchanged and remains
# the single source of truth; only the driver moved.
#
#   scripts/dagger-image.sh --name gateway      --tag gateway:dev
#   scripts/dagger-image.sh --name rest-catalog --tag lance-rest-catalog:dev
#   scripts/dagger-image.sh --zone lakehouse    --tag web-lakehouse:dev
#   scripts/dagger-image.sh --name cnpg-age-ext --tag cnpg-age:dev --arg AGE_REF=v1.5.0
#
# DELIVERY: --load (default) exports an OCI tarball and `docker load`s it, which reproduces exactly what
# `docker buildx build --load` used to do, so `make k3s-import` and the compose stacks keep working
# unchanged. Measured on the gateway image: the export dominates at ~78 s (it is the BUILD), and the
# `docker load` adds ~5 s. --push publishes straight to a registry instead, skipping the daemon.
#
# THE ENGINE. Dagger always speaks HTTPS to a registry and `publish` has no --insecure flag, so pushing
# to the plain-HTTP dev registry needs the engine provisioned by `make dagger-engine`. Only --push needs
# it; --load does not touch a registry. When _EXPERIMENTAL_DAGGER_RUNNER_HOST is already exported we
# respect it, otherwise we point at that engine if it happens to be running, and otherwise we let the
# Dagger CLI auto-provision its own — which is correct for --load and would fail only on --push.
set -euo pipefail

NAME="" ZONE="" TAG="" MODE="load" ADDRESS="" DEV="false"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)    NAME="$2"; shift 2 ;;
    --zone)    ZONE="$2"; shift 2 ;;
    --tag)     TAG="$2"; shift 2 ;;
    --arg)     EXTRA_ARGS+=(--build-arg "$2"); shift 2 ;;
    --dev)     DEV="true"; shift ;;
    --push)    MODE="push"; ADDRESS="$2"; shift 2 ;;
    *) echo "!! unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$NAME" && -z "$ZONE" ]]; then
  echo "!! need --name <dockerfile stem> or --zone <zone>" >&2; exit 2
fi
if [[ "$MODE" == "load" && -z "$TAG" ]]; then
  echo "!! --load needs --tag <image:tag>" >&2; exit 2
fi

command -v dagger >/dev/null 2>&1 || { echo "!! dagger CLI not on PATH — https://docs.dagger.io/install" >&2; exit 1; }

# Respect an operator's choice; otherwise prefer the repo's configured engine when it is up.
if [[ -z "${_EXPERIMENTAL_DAGGER_RUNNER_HOST:-}" ]]; then
  engine="${DAGGER_ENGINE_NAME:-dagger-engine-rask}"
  if [[ "$(docker inspect -f '{{.State.Running}}' "$engine" 2>/dev/null || echo false)" == "true" ]]; then
    export _EXPERIMENTAL_DAGGER_RUNNER_HOST="docker-container://$engine"
  elif [[ "$MODE" == "push" ]]; then
    echo "!! --push needs the insecure-registry engine: run 'make dagger-engine'" >&2; exit 1
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
else
  fn=(image --name="$NAME" --dev="$DEV")
fi
fn+=(--build-date="$BUILD_DATE" --vcs-ref="$VCS_REF" --version="$VERSION")
[[ ${#EXTRA_ARGS[@]} -gt 0 ]] && fn+=("${EXTRA_ARGS[@]}")

if [[ "$MODE" == "push" ]]; then
  echo ">> dagger: ${ZONE:-$NAME} -> $ADDRESS"
  exec dagger call "${fn[@]}" publish --address="$ADDRESS"
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
