# syntax=docker/dockerfile:1.11
# rask frontend image — SvelteKit SSR built with Bun and RUN by the Bun runtime
# (svelte-adapter-bun: ships a Bun *server*, not static files). Build context = repo root.
#
# Parametrized over the workspace app via --build-arg APP=<dir under components/apps>:
#   docker buildx build -f .docker/frontend.dockerfile --build-arg APP=storage-frontend \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) -t storage-frontend:dev .
# APP=frontend builds the catch-all (viewer-frontend); the others build the MFE
# domain apps (each pinned to its base path /default/<domain> in svelte.config.js).
#
# Two bun-1.3 + svelte-adapter-bun gotchas this encodes:
#  1. the adapter externalizes @sveltejs/kit (+ svelte runtime) → ship node_modules
#     (build/ is NOT standalone).
#  2. bun 1.3 isolated linker: real packages live in node_modules/.bun/<pkg>; each
#     workspace member's node_modules holds symlinks INTO that store. So the final
#     image copies the store (root node_modules) AND the app (with its symlinked
#     node_modules) at the path the relative symlinks expect, and runs from the app dir.
# Size note: ships the full node_modules; slimming deferred (correctness first).

# ---- builder: bun install (workspace) + prebuild @rask/ui + bun build --------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f AS builder

ARG APP=frontend
WORKDIR /src

# Every JS workspace member must be present or `bun install --frozen-lockfile`
# errors with "Workspace not found". Keep in sync with package.json "workspaces".
COPY components/apps/frontend          components/apps/frontend
COPY components/apps/storage-frontend  components/apps/storage-frontend
COPY components/apps/compute-frontend  components/apps/compute-frontend
COPY components/apps/discover-frontend components/apps/discover-frontend
COPY components/apps/studio-frontend   components/apps/studio-frontend
COPY components/apps/train-frontend    components/apps/train-frontend
COPY packages/api                      packages/api
COPY packages/ui                       packages/ui
COPY package.json bun.lock             ./

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
    bun run --cwd components/apps/${APP} build

# ---- final: minimal Bun runtime serving the adapter-bun server ---------------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f

ARG APP=frontend
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
COPY --from=builder --chown=10001:10001 /src/components/apps/${APP} ./components/apps/${APP}

# Re-anchor the workdir at the app via a stable symlink so CMD is APP-agnostic.
RUN ln -s "components/apps/${APP}" /app/app
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
