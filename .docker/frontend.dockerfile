# syntax=docker/dockerfile:1.11
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

# Every JS workspace member must be present or `bun install --frozen-lockfile`
# errors with "Workspace not found". Copy all of frontend/microfrontends wholesale so new
# MFE apps don't silently break this build (.dockerignore strips node_modules/
# .svelte-kit). /src IS the frontend workspace root: frontend/* is copied to the root
# of the build stage so bun's workspace globs (microfrontends/*, packages/*) resolve
# unchanged.
COPY frontend/microfrontends microfrontends
COPY frontend/packages packages
COPY frontend/package.json frontend/bun.lock frontend/turbo.json ./
COPY frontend/.oxlintrc.json frontend/.oxfmtrc.json ./
# patchedDependencies (e.g. svelte-adapter-bun) — bun install --frozen-lockfile
# resolves these patch files relative to the project root, so they must be present.
COPY frontend/patches patches

RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

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
# `node_modules/@rask/ui -> ../../../../packages/ui`, i.e. /app/packages/ui — a directory this image did
# not ship, so every @rask/* link in the shipped image DANGLED. It went unnoticed because the SSR bundle
# inlines those packages at build time, so nothing resolves the link at runtime.
#
# It stops being invisible the moment anything re-runs the build inside the container, which is exactly
# what Tilt's live_update does under dev.reload:
#
#   [plugin @tailwindcss/vite:generate:build] /app/microfrontends/home/src/app.css
#   Error: Can't resolve '@rask/ui/styles/tokens.css'
#
# Copying them also gives the live_update `sync('frontend/packages', '/app/packages')` a tree to overlay
# onto — one that already carries packages/ui/dist, which only the builder can produce (svelte-package).
# Sources only: .dockerignore strips node_modules, and dist/ is built above.
COPY --from=builder --chown=10001:10001 /src/packages ./packages

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
