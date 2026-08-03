# syntax=docker/dockerfile:1.11-labs
# rask frontend image — SvelteKit SSR built with Bun and RUN by the Bun runtime
# (svelte-adapter-bun: ships a Bun *server*, not static files). Build context = repo root.
#
# Parametrized over the workspace app via --build-arg APP=<dir under frontend/microfrontends, e.g. home>:
#   docker buildx build -f .docker/frontend.dockerfile --build-arg APP=media \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) -t media:dev .
# APP=home builds the catch-all (home); the others build the MFE
# domain zones (each pinned to its base path /<zone> in svelte.config.js).
#
# Two bun-1.3 + svelte-adapter-bun gotchas this encodes:
#  1. the adapter externalizes @sveltejs/kit (+ svelte runtime) → ship node_modules
#     (build/ is NOT standalone).
#  2. bun 1.3 isolated linker: real packages live in node_modules/.bun/<pkg>; each
#     workspace member's node_modules holds symlinks INTO that store. So the final
#     image copies the store (root node_modules) AND the app (with its symlinked
#     node_modules) at the path the relative symlinks expect, and runs from the app dir.
# Size note: ships the full node_modules; slimming deferred (correctness first).

# ---- builder: bun install (workspace) + prebuild packages/ui + bun build -----
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f AS builder

ARG APP=home
WORKDIR /src

# LAYER ORDER IS THE COST MODEL OF THIS FILE. `bun install --frozen-lockfile` needs every
# workspace member PRESENT — but only its package.json, not its sources. This build used to copy
# all eight zones' sources before the install, so touching ONE zone invalidated the install layer
# of every zone image (~90 s x 8 on any one-line change). Manifests first, install, sources after:
# a source edit now leaves the install layer cached and re-runs only the two build steps.
#
# `--parents` needs the -labs frontend (verified: 1.11 rejects it with "unknown flag"), and the
# `/./` pivot re-roots the copy so `frontend/microfrontends/x/package.json` lands at
# `microfrontends/x/package.json` — /src IS the frontend workspace root, so bun's globs
# (microfrontends/*, packages/*) resolve unchanged. The glob ALSO keeps the old wholesale-copy
# guarantee: a new member is picked up with no edit here, because a path pattern cannot go stale
# the way a hand-kept list can.
COPY frontend/package.json frontend/bun.lock frontend/turbo.json ./
COPY frontend/.oxlintrc.json frontend/.oxfmtrc.json ./
# patchedDependencies (e.g. svelte-adapter-bun) — bun install --frozen-lockfile
# resolves these patch files relative to the project root, so they must be present.
COPY frontend/patches patches
COPY --parents frontend/./microfrontends/*/package.json /src/
COPY --parents frontend/./packages/*/package.json /src/

RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

# Sources AFTER the install, and ONLY this zone's. Every member's package.json is already present
# from the manifest globs above — which is all bun needs the OTHERS for — so copying their sources
# too only re-ran this image's build steps whenever any other zone changed (~67 s x 8 images on a
# one-zone edit; measured 2 s once narrowed). COPY overlays rather than replaces, so the
# node_modules symlink trees bun just created inside each member survive.
# (.dockerignore strips node_modules/.svelte-kit/.turbo.)
COPY frontend/microfrontends/${APP} microfrontends/${APP}
COPY frontend/packages packages

# Pre-build packages/ui (svelte-package → dist/) so its dist exports resolve during
# the app build. The ui svelte.config.js is already @sveltejs/package@2-compatible.
# hadolint ignore=DL3059
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun run --cwd=packages/ui build

# hadolint ignore=DL3059
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun run --cwd=microfrontends/${APP} build

# ---- final: minimal Bun runtime serving the adapter-bun server ---------------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f

ARG APP=home
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-${APP}" \
      org.opencontainers.image.description="rask ${APP} (SvelteKit SSR), Bun server"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends tini

RUN useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app

# Preserve the isolated-linker layout (store + app's symlinked node_modules).
COPY --from=builder --chown=10001:10001 /src/node_modules ./node_modules
COPY --from=builder --chown=10001:10001 /src/microfrontends/${APP} ./microfrontends/${APP}
# The workspace packages, and they are NOT optional. Bun's isolated linker gives the app
# `node_modules/<scope>/ui -> ../../../../packages/ui`, i.e. /app/packages/ui — a directory this image did
# not ship, so every scoped-package link in the shipped image DANGLED. It went unnoticed because the SSR bundle
# inlines those packages at build time, so nothing resolves the link at runtime.
#
# It stops being invisible the moment anything re-runs the build inside the container, which is exactly
# what Tilt's live_update does under dev.reload:
#
#   [plugin @tailwindcss/vite:generate:build] /app/microfrontends/home/src/app.css
#   Error: Can't resolve '<scope>/ui/styles/tokens.css'
#
# Copying them also gives the live_update `sync('frontend/packages', '/app/packages')` a tree to overlay
# onto — one that already carries packages/ui/dist, which only the builder can produce (svelte-package).
# Sources only: .dockerignore strips node_modules, and dist/ is built above.
COPY --from=builder --chown=10001:10001 /src/packages ./packages

# The dev entrypoint. Shipped in every image but only USED when the chart selects it under dev.reload,
# so a production zone still runs the plain `bun build/index.js` CMD below. A few hundred bytes.
COPY --chown=10001:10001 .docker/dev-serve.sh /usr/local/bin/dev-serve.sh

# Re-anchor the workdir at the app via a stable symlink so CMD is APP-agnostic.
RUN ln -s "microfrontends/${APP}" /app/app
WORKDIR /app/app

USER 10001
ENV PORT=3000 \
    HOST=0.0.0.0 \
    HOME=/tmp
EXPOSE 3000

# Hit "/" and accept any non-5xx: domain apps have a base path so "/" is 404 (<500),
# which still proves the server is alive without hardcoding each app's base.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD bun -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["tini", "--"]
CMD ["bun", "build/index.js"]
