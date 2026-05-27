# syntax=docker/dockerfile:1.11
# rask frontend image — SvelteKit adapter-static built with Bun, served by nginx-unprivileged.
# Install to repo root as:
#   cp .claude/skills/dockerfile/templates/frontend.dockerfile .docker/frontend.dockerfile
#   cp .claude/skills/dockerfile/templates/frontend.nginx.conf .docker/frontend.nginx.conf
# Build:
#   docker buildx build -f .docker/frontend.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t frontend:dev .
#
# Note: the nginx config has `gzip_static on;` so it can serve pre-compressed
# .gz files emitted by adapter-static `precompress: true`. The current
# svelte.config.js does NOT enable precompress — turning it on in
# components/apps/frontend/svelte.config.js is a one-line opt-in if you
# want gzip-precompressed assets.
#
# Deviation from base template: packages/component-lib (@your-repo/oxen) exports
# from ./dist/ (svelte-package output) which does not exist as source. A pre-build
# step `bun --cwd packages/component-lib run build` is required before the frontend
# build so module resolution succeeds.

# ---- builder stage: bun install (workspace) + bun build --------------------
FROM oven/bun:1-debian@sha256:9dba1a1b43ce28c9d7931bfc4eb00feb63b0114720a0277a8f939ae4dfc9db6f AS builder

WORKDIR /src

# COPY workspace sources + lock metadata first.
COPY components/apps/frontend components/apps/frontend
COPY packages/component-lib   packages/component-lib
COPY package.json bun.lock    ./

# Install workspace deps. Bind-mount pattern avoided here because bun install
# writes node_modules into WORKDIR — COPY above must precede to avoid conflicts.
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

# Pre-build component-lib so @your-repo/oxen dist/ exports are resolvable.
# packages/component-lib/svelte.config.js carries `config.package` which @sveltejs/package@2
# rejects with a hard error. Replace it with a minimal valid config before packaging.
# `package` script = svelte-package only (skips publint which is a dev/CI concern).
# hadolint ignore=DL3059
RUN --mount=type=cache,target=/root/.bun/install/cache \
    printf "import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';\nexport default { preprocess: vitePreprocess() };\n" \
      > packages/component-lib/svelte.config.js \
    && bun run --cwd packages/component-lib package

# Two separate RUN layers: component-lib must build before frontend (dist/ exports).
# hadolint ignore=DL3059
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun run --cwd components/apps/frontend build

# ---- final stage: nginx-unprivileged serves the static build --------------
FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-frontend" \
      org.opencontainers.image.description="rask SvelteKit SPA served by nginx-unprivileged"

# nginx-unprivileged sets USER 101 in the base image. We need root to write to
# /etc/nginx/nginx.conf and /usr/share/nginx/html/, then drop back to 101.
USER root

# Install the full nginx config (replaces /etc/nginx/nginx.conf because it includes
# top-level pid/http/events directives the read-only-rootfs design needs).
# .docker/ is excluded by .dockerignore so COPY is not possible; the config is
# embedded as a heredoc. The canonical source is
#   .claude/skills/dockerfile/templates/frontend.nginx.conf
# Keep in sync when editing.
# hadolint ignore=DL3059
RUN --network=none <<'EOF'
cat > /etc/nginx/nginx.conf << 'NGINX'
# rask frontend nginx config — SvelteKit adapter-static SPA + nginx-unprivileged.
# Image runs as UID 101 and supports --read-only --tmpfs /tmp.

# Read-only-rootfs path overrides (writable paths must be in /tmp).
pid /tmp/nginx.pid;

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # Writable paths inside the read-only rootfs.
    client_body_temp_path /tmp/client_body;
    proxy_temp_path       /tmp/proxy;
    fastcgi_temp_path     /tmp/fastcgi;
    uwsgi_temp_path       /tmp/uwsgi;
    scgi_temp_path        /tmp/scgi;

    sendfile        on;
    tcp_nopush      on;
    tcp_nodelay     on;
    keepalive_timeout  65;

    # gzip — Brotli is non-trivial on nginx-unprivileged (no ngx_brotli module).
    # adapter-static `precompress: true` emits .gz files; gzip_static serves them.
    gzip              on;
    gzip_vary         on;
    gzip_static       on;
    gzip_types        text/css application/javascript application/json image/svg+xml;

    server {
        listen 8080;
        server_name _;
        root   /usr/share/nginx/html;
        index  index.html;

        # CSP-friendly base. Svelte 5 emits inline `__e=event` handlers (svelte#14014);
        # let SvelteKit's csp.directives in svelte.config.js own the policy — this header
        # is the minimum-viable default that doesn't break dev.
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;

        # /_app/version.json — MUST be no-cache so SvelteKit's deploy-reload polling works.
        # Placed ABOVE the immutable block (kit#3194, #15150).
        location = /_app/version.json {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }

        # Hashed assets in /_app/immutable/ get a year of caching.
        location ^~ /_app/immutable/ {
            add_header Cache-Control "public, max-age=31536000, immutable" always;
            try_files $uri =404;
        }

        # Block accidental dotfile exposure (.env, .git, .DS_Store) — but allow .well-known.
        location ~ /\.(?!well-known) {
            deny all;
        }

        # SPA fallback: prerendered .html -> /foo/index.html -> client-rendered shell.
        location / {
            add_header Cache-Control "no-cache" always;
            try_files $uri $uri.html $uri/index.html /index.html;
        }
    }
}

events {}
NGINX
EOF

# Static assets — RUN --network=none preserves "no exfil" on the final copy.
# cp without -a: don't try to preserve root ownership from builder into html dir.
RUN --network=none --mount=from=builder,source=/src/components/apps/frontend/build,target=/tmp/build \
    cp -r /tmp/build/. /usr/share/nginx/html/

# Drop back to nginx-unprivileged's runtime user.
USER 101

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1:8080/ >/dev/null 2>&1 || exit 1

# nginx-unprivileged already has its own init; no tini needed.
