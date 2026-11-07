# syntax=docker/dockerfile:1.11-labs
# rask console image — the console single-page app (SvelteKit, adapter-static), built with Bun and
# served by its own static server, `frontend/console/serve.ts`. Build context = repo root.
#
# The image holds the built files and the server, nothing else: no node_modules (serve.ts uses only
# Bun and node:path), no credential, no environment. The release mounts `/config.json` (the issuer,
# client and scopes) and names it with CONSOLE_CONFIG_FILE.
#
# Built by Dagger through scripts/dagger-image.sh, the one build seam (never docker).

# ---- builder: bun install (workspace) + prebuild packages/ui + build the console -----
# The bun the workspace pins (frontend/package.json `packageManager`, .dagger/frontend.go's lanes), so the image builds
# with the toolchain its gates ran. Of this stage only the build, the bun binary and tini-static ship.
FROM oven/bun:1.4.4-debian@sha256:93d5032fe06d163427822dc1598f420363759bb18155c9cc90eb1be05a64a19e AS builder

# tini-static for the runtime stage, which has no package manager to install it with.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends tini

WORKDIR /src

# LAYER ORDER IS THE COST MODEL OF THIS FILE. `bun install --frozen-lockfile` needs every workspace
# member PRESENT — but only its package.json, not its sources. Manifests first, install, sources after:
# a source edit leaves the install layer cached and re-runs only the build steps.
#
# `--parents` needs the -labs frontend, and the `/./` pivot re-roots the copy so
# `frontend/packages/x/package.json` lands at `packages/x/package.json` — /src IS the frontend
# workspace root, so bun's globs resolve unchanged and a new package is picked up with no edit here.
COPY frontend/package.json frontend/bun.lock ./
COPY frontend/.oxlintrc.json frontend/.oxfmtrc.json ./
# patchedDependencies — bun install --frozen-lockfile resolves these relative to the project root.
COPY frontend/patches patches
COPY frontend/console/package.json console/package.json
COPY --parents frontend/./packages/*/package.json /src/

RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

# Sources AFTER the install. COPY overlays rather than replaces, so the node_modules symlink trees bun
# just created inside each member survive (.dockerignore strips node_modules/.svelte-kit/build).
COPY frontend/console console
COPY frontend/packages packages

# Pre-build packages/ui (svelte-package → dist/) so its dist exports resolve during the app build.
# hadolint ignore=DL3059
RUN bun run --cwd=packages/ui build

# hadolint ignore=DL3059
RUN bun run --cwd=console build

# ---- final: the bun binary on distroless, serving the built files ---------------
# Distroless rather than an oven/bun runtime tag: the server needs only bun and glibc (`ldd /usr/local/bin/bun` lists
# libc, libpthread, libdl and libm), and an oven/bun tag is built once per bun release and never refreshed, so its
# Debian packages age with the bun version the workspace pins. distroless/base-nossl is glibc, CA certificates and
# tzdata with no shell, package manager or OpenSSL, rebuilt by Google as Debian publishes fixes.
# hadolint ignore=DL3026  # Reason: gcr.io/distroless is Google's official distroless registry; digest-pinned.
FROM gcr.io/distroless/base-nossl-debian13@sha256:d710a5c8bfe4cf7e726d7e1afd73e363a9fb02c2a11ad893d2d6185c1b9a89cb

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-console" \
      org.opencontainers.image.description="rask console: the single-page app and its static server"

# tini as PID 1, so SIGTERM reaches the server and a rollout drains it.
COPY --from=builder /usr/bin/tini-static /usr/bin/tini
COPY --from=builder /usr/local/bin/bun /usr/local/bin/bun

WORKDIR /app

# Read-only at runtime: the server reads these and writes nothing, so a read-only root filesystem holds.
COPY --from=builder --chown=0:0 /src/console/build ./build
COPY --from=builder --chown=0:0 /src/console/serve.ts ./serve.ts

# uid 10001, as the lakehouse's image runs, and the product chart's `console.runAsUser` (chart/values.yaml): a non-root
# uid of 10000 or more. Numeric, so the kubelet can verify `runAsNonRoot`; the uid needs no passwd entry, and HOME points
# at the chart's /tmp mount.
USER 10001
# BUN_RUNTIME_TRANSPILER_CACHE_PATH=0 keeps bun from writing a transpiler cache, which a read-only root refuses.
ENV PORT=3000 \
    HOME=/tmp \
    BUN_RUNTIME_TRANSPILER_CACHE_PATH=0
EXPOSE 3000

# Exec form: the image has no shell.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD ["bun", "-e", "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

ENTRYPOINT ["tini", "--"]
CMD ["bun", "serve.ts"]
