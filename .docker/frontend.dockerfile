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
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f AS builder

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

# ---- final: the Bun runtime serving the built files ---------------------------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f

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
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends tini

WORKDIR /app

# Read-only at runtime: the server reads these and writes nothing, so a read-only root filesystem holds.
COPY --from=builder --chown=0:0 /src/console/build ./build
COPY --from=builder --chown=0:0 /src/console/serve.ts ./serve.ts

# The base image's `bun` user (uid 1000), which the console release runs as.
USER 1000
ENV PORT=3000 \
    HOME=/tmp
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD bun -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["tini", "--"]
CMD ["bun", "serve.ts"]
