# syntax=docker/dockerfile:1.11
# rask frontend image — SvelteKit SSR built with Bun and RUN by the Bun runtime.
# (Was nginx-static for adapter-static; the app is now svelte-adapter-bun SSR, so it
# ships a Bun *server*, not static files.) Build context = repo root.
#   docker buildx build -f .docker/frontend.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) -t frontend:dev .
#
# See .docker/storage-frontend.dockerfile for the two bun-1.3 gotchas this encodes
# (adapter externalizes @sveltejs/kit → ship node_modules; isolated linker → preserve
# the .bun store + the app's symlinked node_modules, run from the app dir).
# Size note: ships the full node_modules; slimming deferred (correctness first).

# ---- builder: bun install (workspace) + prebuild @rask/ui + bun build --------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f AS builder

WORKDIR /src

# Full source of every workspace member so `bun install` + prepare scripts resolve.
COPY components/apps/frontend         components/apps/frontend
COPY components/apps/storage-frontend components/apps/storage-frontend
COPY components/apps/compute-frontend components/apps/compute-frontend
COPY packages/api                     packages/api
COPY packages/ui                      packages/ui
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
    bun run --cwd components/apps/frontend build

# ---- final: minimal Bun runtime serving the adapter-bun server ---------------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-frontend" \
      org.opencontainers.image.description="rask frontend (SvelteKit SSR), Bun server"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends tini

RUN useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app

# Preserve the isolated-linker layout (store + app's symlinked node_modules).
COPY --from=builder --chown=10001:10001 /src/node_modules ./node_modules
COPY --from=builder --chown=10001:10001 /src/components/apps/frontend ./components/apps/frontend

WORKDIR /app/components/apps/frontend

USER 10001
ENV PORT=3000 \
    HOST=0.0.0.0 \
    HOME=/tmp
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD bun -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["tini", "--"]
CMD ["bun", "build/index.js"]
