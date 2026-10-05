# syntax=docker/dockerfile:1.11
# The object store's server and its admin client, compiled from pinned upstream source.
#
# WHY THE ESTATE BUILDS THESE BYTES. `minio/minio` and `minio/mc` refuse anonymous pulls on Docker Hub
# and quay.io (measured 2026-09-24 and again 2026-10-05: 401 on the manifest for both tags below), so a
# node with an empty image cache can neither start the store nor run the scoped-users hook, and the
# first upgrade there dies in `pre-upgrade` ([[XC-075]], owner decision D10). Upstream still publishes the
# source and its tags; this image is built from them, so no registry outside the estate can withdraw it.
#
# ONE IMAGE, TWO BINARIES, the layout upstream's own release image used (Dockerfile.release copies both
# `minio` and `mc` into /usr/bin): the server runs `minio` through the ENTRYPOINT, and the
# minio-scoped-users hook runs `sh -c` with `mc` on PATH, so one tag and one push serve both.
#
#   scripts/dagger-image.sh --name minio --tag minio:dev
#   dagger call image --name=minio publish --address=172.17.0.1:5000/minio:<tag>
#
# THE PINS ARE A TAG AND THE COMMIT IT NAMES. The clone is by tag and the build refuses to continue
# unless HEAD is the recorded commit, so a moved tag fails the build instead of shipping other code.
# The server matches the version the estate runs (RELEASE.2025-04-22T22-12-26Z on rask-minio,
# measured 2026-10-05); mc matches the tag the chart named. Module downloads are checked against
# each repo's go.sum through the Go checksum database.
ARG MINIO_TAG=RELEASE.2025-04-22T22-12-26Z
ARG MINIO_COMMIT=0d7408fc9969caf07de6a8c3a84f9fbb10a6739e
ARG MC_TAG=RELEASE.2025-08-13T08-35-41Z
ARG MC_COMMIT=7394ce0dd2a80935aded936b09fa12cbb3cb8096

# ── minio-build: the server binary, from source at the pinned commit ──────────────────────────────────
# Go 1.26.8, a supported release with current stdlib fixes; the modules ask for go >= 1.24 (minio)
# and >= 1.23 (mc), and GOTOOLCHAIN=local stops `go` fetching the older toolchain go.mod names.
# hadolint ignore=DL3026  # Reason: golang is the official Docker Hub image; digest-pinned for reproducibility.
FROM golang:1.26.8-bookworm@sha256:a688600ca24f8a4d3ca77f95b0dd40704a9fc787c826660eb7ba0b641b8b175d AS minio-build
ARG MINIO_TAG
ARG MINIO_COMMIT
ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local
WORKDIR /src
RUN git clone --depth 1 --branch "${MINIO_TAG}" https://github.com/minio/minio . \
 && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"
# The ldflags are upstream's own (buildscripts/gen-ldflags.go), with the release prefix and the
# tag's timestamp, so `minio --version` and the admin API report the release this was built from.
# The ARG is MINIO_TAG and not MINIO_RELEASE because gen-ldflags reads MINIO_RELEASE as the prefix.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    stamp="${MINIO_TAG#RELEASE.}" \
 && version="${stamp%%T*}T$(printf '%s' "${stamp#*T}" | tr '-' ':')" \
 && ldflags="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go "${version}")" \
 && go build -tags kqueue -trimpath -ldflags "${ldflags}" -o /out/minio . \
 && /out/minio --version

# ── mc-build: the admin client the scoped-users hook drives, from source at the pinned commit ─────────
# hadolint ignore=DL3026  # Reason: golang is the official Docker Hub image; digest-pinned for reproducibility.
FROM golang:1.26.8-bookworm@sha256:a688600ca24f8a4d3ca77f95b0dd40704a9fc787c826660eb7ba0b641b8b175d AS mc-build
ARG MC_TAG
ARG MC_COMMIT
ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local
WORKDIR /src
RUN git clone --depth 1 --branch "${MC_TAG}" https://github.com/minio/mc . \
 && test "$(git rev-parse HEAD)" = "${MC_COMMIT}"
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    stamp="${MC_TAG#RELEASE.}" \
 && version="${stamp%%T*}T$(printf '%s' "${stamp#*T}" | tr '-' ':')" \
 && ldflags="$(MC_RELEASE=RELEASE go run buildscripts/gen-ldflags.go "${version}")" \
 && go build -tags kqueue -trimpath -ldflags "${ldflags}" -o /out/mc . \
 && /out/mc --version

# ── final: the two static binaries on a shell base ────────────────────────────────────────────────────
# A SHELL IS PART OF THE CONTRACT: the scoped-users hook is a `sh -c` script (heredoc policies, `cat`,
# `sleep`), so a scratch or distroless base would break it. Alpine's busybox supplies exactly that.
# hadolint ignore=DL3026  # Reason: alpine is the official Docker Hub image; digest-pinned for reproducibility.
FROM alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
ARG MINIO_TAG
ARG MC_TAG
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="minio" \
      org.opencontainers.image.description="MinIO server ${MINIO_TAG} and mc ${MC_TAG}, compiled from upstream source" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# `minio-user` at uid 1000 is the identity `security.infraContexts.minio` pins (values.yaml), so that
# uid resolves to a named account. The package manager leaves with the setuid bits: nothing installs
# into this image after it is built.
RUN adduser -D -H -u 1000 -s /sbin/nologin minio-user \
 && rm -rf /sbin/apk /etc/apk /lib/apk /usr/share/apk /var/cache/apk \
 && { find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true; }

COPY --from=minio-build /out/minio /usr/bin/minio
COPY --from=mc-build /out/mc /usr/bin/mc
COPY --from=minio-build /src/LICENSE /licenses/minio/LICENSE
COPY --from=minio-build /src/CREDITS /licenses/minio/CREDITS
COPY --from=mc-build /src/LICENSE /licenses/mc/LICENSE
COPY --from=mc-build /src/CREDITS /licenses/mc/CREDITS

# mc keeps its alias config here; /tmp is the writable mount every pod in the chart gives it.
ENV MC_CONFIG_DIR=/tmp/.mc

# NO `USER` LINE, ON PURPOSE: the default identity is root, the one upstream's image ran as and the one
# rask-minio runs as today (`security.infraContexts.enabled: false`, so the pod sets no runAsUser; the
# live data under /data-* was written by uid 0). An image that switched to uid 1000 by default would
# start, read, and fail every write against that data. The non-root move belongs to the chart, through
# `security.infraContexts.minio`, together with a chown of the volumes; the hook already runs as 65532.
EXPOSE 9000 9001
ENTRYPOINT ["/usr/bin/minio"]
