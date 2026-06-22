# syntax=docker/dockerfile:1.11
# rask storage-frontend microfrontend — SvelteKit SSR built with Bun and RUN by the
# Bun runtime (svelte-adapter-bun). Build context = repo root.
#   docker buildx build -f .docker/storage-frontend.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) -t storage-frontend:dev .
#
# Two non-obvious things about bun 1.3 + svelte-adapter-bun:
#  1. svelte-adapter-bun externalizes @sveltejs/kit (+ svelte runtime), so the final
#     image must ship node_modules — build/ is NOT standalone.
#  2. bun 1.3 uses an ISOLATED linker: real packages live in node_modules/.bun/<pkg>,
#     and each workspace member's node_modules holds symlinks INTO that store. So the
#     final image copies the store (root node_modules) AND the app (with its symlinked
#     node_modules) at the paths the relative symlinks expect, then runs from the app
#     dir so module resolution walks into the store.
#  Size note: this ships the full (dev+prod) node_modules. Slimming needs a standalone
#  bundle or careful dep re-categorization (svelte is a runtime need but dev-scoped);
#  deferred — correctness first.

# ---- builder: bun install (workspace) + prebuild @rask/ui + bun build --------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f AS builder

WORKDIR /src

# Full source of every workspace member so `bun install` (and members' prepare
# scripts) resolve. Siblings are build-stage only — never shipped.
COPY components/apps/frontend         components/apps/frontend
COPY components/apps/storage-frontend components/apps/storage-frontend
COPY packages/ui           packages/ui
COPY package.json bun.lock            ./

RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

# Pre-build @rask/ui so its dist/ exports resolve during the app build.
# @sveltejs/package@2 rejects the `config.package` key — swap a minimal config in.
# hadolint ignore=DL3059
RUN --mount=type=cache,target=/root/.bun/install/cache \
    printf "import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';\nexport default { preprocess: vitePreprocess() };\n" \
      > packages/ui/svelte.config.js \
    && bun run --cwd packages/ui package

# hadolint ignore=DL3059
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun run --cwd components/apps/storage-frontend build

# ---- final: minimal Bun runtime serving the adapter-bun server ---------------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-storage-frontend" \
      org.opencontainers.image.description="rask Storage microfrontend (SvelteKit SSR, base /storage), Bun server"

# tini as PID 1 — signal forwarding + zombie reaping for the long-lived server.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends tini

RUN useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app

# Preserve the isolated-linker layout: the store (root node_modules) at /app/node_modules,
# the app (with its symlinked node_modules + build/) at its original relative depth so the
# `../../../../../node_modules/.bun` symlinks resolve back to /app/node_modules/.bun.
COPY --from=builder --chown=10001:10001 /src/node_modules ./node_modules
COPY --from=builder --chown=10001:10001 /src/components/apps/storage-frontend ./components/apps/storage-frontend

WORKDIR /app/components/apps/storage-frontend

USER 10001
# svelte-adapter-bun reads PORT/HOST. HOME=/tmp = writable bun cache as UID 10001.
ENV PORT=3000 \
    HOST=0.0.0.0 \
    HOME=/tmp
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD bun -e "fetch('http://127.0.0.1:3000/storage').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["tini", "--"]
CMD ["bun", "build/index.js"]
