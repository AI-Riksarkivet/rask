# Static bundle + nginx (rask)

Patterns for static-asset frontends (SvelteKit adapter-static, Vite, etc.) served by nginx-unprivileged.

## Why nginx, not FastAPI, for static assets

nginx's `sendfile` syscall transfers file bytes directly from kernel page-cache to the socket, bypassing userspace entirely. For static assets this means near-zero CPU overhead per request. Combined with first-class support for `Cache-Control: immutable` headers and `gzip_static on` (serving pre-compressed `.gz` files without runtime CPU), nginx can serve a frontend at roughly 5 MB RAM per worker process.

FastAPI/Starlette can serve static files via `StaticFiles`, but every request still passes through the Python event loop and ASGI machinery. This adds latency for no benefit when the response is just a byte-for-byte file read. Keeping the frontend in its own nginx container also lets each tier scale and deploy independently. See SvelteKit community patterns around kit#15150 for the standard split.

## Builder: oven/bun:1-debian + cache mount

Pin the builder by digest in production to get reproducible builds:

```dockerfile
FROM oven/bun:1-debian AS builder
```

Mount the Bun install cache to avoid re-downloading packages on every build:

```dockerfile
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile
```

`--frozen-lockfile` fails fast if `bun.lock` is out of date rather than silently modifying it. After install, run `bun run build`. SvelteKit's adapter-static writes the production bundle to `build/` by default.

## Build context is repo root

Bun workspaces require the root `package.json`, the root `bun.lock`, and each workspace member's `package.json` before the install step can resolve the dependency graph. The rask workspace has two members:

- `components/apps/frontend/package.json`
- `packages/component-lib/package.json`

Use bind mounts for the install step to keep the layer narrow, then COPY sources for the build step:

```dockerfile
RUN --mount=type=bind,source=package.json,target=package.json \
    --mount=type=bind,source=bun.lock,target=bun.lock \
    --mount=type=bind,source=components/apps/frontend/package.json,target=components/apps/frontend/package.json \
    --mount=type=bind,source=packages/component-lib/package.json,target=packages/component-lib/package.json \
    --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

COPY components/apps/frontend/ components/apps/frontend/
COPY packages/component-lib/ packages/component-lib/

RUN bun --cwd components/apps/frontend run build
```

The lockfile is `bun.lock` (text format), not `bun.lockb` (binary). Sending the build context from repo root means `.dockerignore` should exclude `node_modules`, `.git`, and other large trees.

## Runtime: nginxinc/nginx-unprivileged:1.27-alpine

```dockerfile
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
```

The unprivileged image already runs as UID 101 and listens on port 8080 (not 80). The PID file is written to `/tmp/nginx.pid`. No `USER` directive is needed — the image's entrypoint handles it. Alpine keeps the final image under 10 MB and ships `wget`, which is useful for `HEALTHCHECK`.

Pin the minor version (`1.27`) for stability; update it in a scheduled maintenance window to pick up security patches.

## Read-only-rootfs nginx config

To run the container with `--read-only --tmpfs /tmp`, every path nginx writes to at runtime must point into `/tmp`. Override the defaults in your `nginx.conf`:

```nginx
pid /tmp/nginx.pid;

http {
    client_body_temp_path /tmp/client_body;
    proxy_temp_path        /tmp/proxy;
    fastcgi_temp_path      /tmp/fastcgi;
    uwsgi_temp_path        /tmp/uwsgi;
    scgi_temp_path         /tmp/scgi;
    ...
}
```

Without these overrides nginx attempts to write to `/var/cache/nginx/` and `/run/`, which fail under a read-only rootfs and cause cryptic startup errors. The `/tmp` tmpfs also acts as the only writable surface, bounding the blast radius of any file-write vulnerability to ephemeral storage.

## SPA `try_files` fallback

SvelteKit adapter-static pre-renders pages as `/foo.html` (or `/foo/index.html` depending on `trailingSlash`). For client-rendered fallback routes, the bundle includes an `index.html` at the root. The nginx location block must handle all three cases:

```nginx
location / {
    root       /usr/share/nginx/html;
    try_files  $uri $uri.html $uri/index.html /index.html;
    add_header Cache-Control "no-cache";
}
```

The `no-cache` on the root location ensures browsers always revalidate HTML documents, so a new deploy is picked up promptly. Assets with hashed filenames in `/_app/immutable/` get their own long-lived cache header (see next section).

## `/_app/immutable/` cache

SvelteKit writes all fingerprinted JS, CSS, and font assets under `/_app/immutable/`. Because the filenames include content hashes, it is safe to cache them indefinitely:

```nginx
location ^~ /_app/immutable/ {
    root       /usr/share/nginx/html;
    add_header Cache-Control "public, max-age=31536000, immutable";
}
```

`^~ ` gives this block prefix-match priority over regex locations. The `immutable` directive tells supporting browsers they never need to revalidate the asset, eliminating conditional-GET round trips on repeat visits.

## `/_app/version.json` no-cache override

SvelteKit polls `/_app/version.json` to detect when a new build has been deployed. If this file is served with an immutable header (because it falls under `/_app/`), the browser will cache the old version indefinitely and never trigger the reload-on-deploy behaviour. Place this block **above** the `/_app/immutable/` block so it takes precedence:

```nginx
location = /_app/version.json {
    root       /usr/share/nginx/html;
    add_header Cache-Control "no-cache";
}
```

See sveltejs/kit#3194 and sveltejs/kit#15150 for the original discussions. The exact-match (`=`) makes the ordering unambiguous even if the immutable block were reordered.

## Block dotfiles except `/.well-known/`

adapter-static may legitimately emit `.well-known/` (ACME challenge files) and `.nojekyll`. Other dotfiles such as `.env`, `.git`, or `.DS_Store` should never be reachable if the `.dockerignore` misses something. Add a negative-lookahead regex location:

```nginx
location ~ /\.(?!well-known) {
    deny all;
}
```

Place this after the `/.well-known/` exemption intent is clear. nginx evaluates regex locations in declaration order after prefix matches, so ordering matters. This rule catches any dotfile path that does not continue with `well-known`.

## Brotli is non-trivial on nginx-unprivileged

The official `nginxinc/nginx-unprivileged` image does not ship the `ngx_brotli` module. There are two paths:

**(a) Gzip-only (recommended default).** Enable `precompress: true` in `svelte.config.js` under the adapter-static options. Vite/Rollup will emit `.gz` siblings for every asset. Then add `gzip_static on;` to the nginx config so nginx serves the pre-compressed file directly without runtime CPU:

```nginx
gzip_static on;
```

No derived base image, no module compilation, no maintenance burden.

**(b) Brotli via a layered base.** Use `fholzer/docker-nginx-brotli` as the runtime base and configure `brotli_static on;`. This yields 10-20% better compression than gzip for JS/CSS at the cost of maintaining a non-official base image and auditing it for security patches. Choose this path only if measured TTFB improvements (run Lighthouse / WebPageTest first) justify the extra maintenance.

## Svelte 5 CSP gotcha

Svelte 5 still emits inline event handlers in compiled output — for example `__e=event` attributes on `<img>` elements (svelte#14014). A strict `Content-Security-Policy: script-src 'strict-dynamic'` header served from nginx will block these and break interactivity.

There are two remediation paths:

**SvelteKit-side (recommended).** Use the `csp` option in `svelte.config.js`. SvelteKit computes a nonce or hash for each inline script per build and injects it into the HTML `<meta>` CSP tag or a `Content-Security-Policy` header. The framework becomes the source of truth; the nginx config just passes the header through without trying to enumerate hashes.

**nginx-side fallback.** Include `'unsafe-hashes'` plus the SHA-256 hashes of the specific inline event-handler snippets in the nginx `Content-Security-Policy` header. This is fragile: hashes change when Svelte's compiler output changes, so the nginx config must be updated on every Svelte upgrade.

The nginx config in the skill provides a CSP-friendly base (no `unsafe-inline`, no wildcard sources) but does not ship a complete CSP header — SvelteKit owns that value.

## HEALTHCHECK via wget

Alpine ships `wget` without needing an additional package. A minimal healthcheck:

```dockerfile
HEALTHCHECK CMD wget -qO- http://127.0.0.1:8080/ || exit 1
```

`-q` suppresses progress output; `-O-` writes the response body to stdout (discarded). The check probes nginx directly on the loopback interface. This is optional when an external orchestrator (Kubernetes liveness probe, Docker Swarm healthcheck) already covers readiness — but useful for `docker compose` local setups where there is no orchestrator layer.

## Why a separate frontend container, not served from viewer

Serving the SvelteKit bundle from the viewer (FastAPI) would couple the two deploy lifecycles together. With a separate frontend container:

- A frontend change (UI fix, new component) can be deployed without rolling the viewer API pods, avoiding downtime for active streaming connections.
- A viewer API change (new endpoint, dependency upgrade) does not invalidate client-side asset caches or force a frontend redeploy.
- The nginx performance arguments from section 1 apply: static file serving belongs in a purpose-built server, not an async Python framework.

The split maps cleanly to rask's repo structure — `components/apps/frontend/` and the viewer live in different directories and have independent `package.json` / `pyproject.toml` manifests.
