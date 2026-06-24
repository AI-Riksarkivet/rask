# Design: MFE production correctness (assets + API redirect) + e2e guard

Date: 2026-06-24
Status: approved (brainstorm) → ready for writing-plans

## Problem

Playwright browser testing of the live k3s deploy surfaced two independent,
previously-undetected production bugs. Neither is visible to `curl`/TCP probes
(SSR HTML returns 200), and both were masked in development by the dev toolchain.

### Bug A — domain microfrontends never hydrate (assets 404)

`svelte-adapter-bun@1.0.1` is **internally inconsistent** with `kit.paths.base`
(`/default/<domain>`) in its built server (`build/handler.js`):

- The static-asset server (`sirv`) is rooted at `client${base}` but matches
  requests at **root** URLs. It therefore only serves `/_app/...`, not
  `/default/<domain>/_app/...`.
- The SSR page handler requires the base to be **present**: `GET /` → 404,
  `GET /default/<domain>/` → 200.

So no single proxy behavior satisfies both halves of the adapter. SvelteKit emits
relative asset refs that the browser resolves to `/default/<domain>/_app/...`,
which the adapter 404s → client JS never loads → the page is dead SSR HTML. The
catch-all viewer escapes it only because its base is `/`.

This contradicts the intended deployment model (`deployment.md`,
`frontend-microfrontends.md` §Deployment): "in k3s each app is a
Deployment+Service; the ingress routes `/default/<domain>` to its app and the app
self-serves." The adapter does not hold up its end.

### Bug B — API calls die on an internal-URL redirect (Dapr regression)

The frontend is correct: per the canon, reads are server-only remote `query()`
calling `@rask/api` via `getRequestEvent().fetch` with **relative** `/api/*`. The
failure is in the gateway path:

1. The request reaches the gateway as `/api/batches/` (trailing slash intact).
2. The gateway proxies via **Dapr service invocation**
   (`/v1.0/invoke/core-api/method/api/batches/`).
3. **Dapr drops the trailing slash** — proven via core-api's access log:
   a traced `GET /api/batches/?trace=…` arrived at core-api as
   `GET /api/batches?trace=… → 307`.
4. core-api's FastAPI `redirect_slashes=True` issues a **307** to
   `Location: http://127.0.0.1:8801/api/batches/` — an absolute URL built from
   core-api's own in-pod address (as seen through the daprd sidecar).
5. The gateway forwards that 307 + internal `Location` **verbatim**
   (`gateway/__init__.py:154`; `httpx` defaults to `follow_redirects=False`,
   `Location` is not rewritten).
6. The follower (browser, or — in the new server-side model — the frontend's SSR
   `getRequestEvent().fetch`) follows to `127.0.0.1:8801`, which is unreachable
   from the caller → `ERR_CONNECTION_REFUSED` / `error(502)`.

`/api/ray/jobs` works only because it has no trailing slash, so no redirect fires.
This is a regression introduced by the Dapr integration: without Dapr the httpx
path preserved the slash.

## Goals

- Domain MFEs fully hydrate in a real browser behind the k3s ingress, with assets
  served under each app's base path — **keeping `svelte-adapter-bun`** (the canon's
  mandated adapter).
- `/api/*` calls through the gateway succeed with **no internal-URL leak and no
  spurious redirect**, with Dapr service invocation still in use.
- A permanent, runnable browser e2e test guards both so they cannot silently
  return ("verify like it ships").
- Remove the temporary Traefik asset-strip middleware (the band-aid).

## Non-goals

- Switching adapters (adapter-node is rejected: violates the canon).
- Deploying the Turborepo microfrontends proxy into k3s (larger architecture
  change; deferred).
- Changing the data-fetching canon (remote `query()` is correct).
- Multi-project / dynamic base paths (already deferred by the canon).

## Design

### Component 1 — Fix the adapter (Bug A)

Patch `svelte-adapter-bun` with **`bun patch`** so its static server roots at
`client` instead of `client${base}`:

```
- var asset_dir = `${import.meta.dir}/client${base}`;
+ var asset_dir = `${import.meta.dir}/client`;
```

With sirv rooted at `client` and matching the full request path:
- `/default/<domain>/_app/...` → `client/default/<domain>/_app/...` → **200**
- `/default/<domain>/` → no static match → falls through to the SSR handler → **200**
- the catch-all viewer (base `/`) is unchanged (`client` + `/...`).

The adapter becomes self-consistent: pages **and** assets are served under the
base, so the app is correct behind any proxy that routes `/default/<domain>` → app
(exactly what the chart's ingress already does). The patch is committed
(`patches/` + `patchedDependencies` in the root `package.json`) so
`bun install --frozen-lockfile` in `.docker/frontend.dockerfile` applies it at
build time — it travels with the build.

File an upstream PR to `svelte-adapter-bun`; once merged + released we bump and
drop the patch. A `// PATCH:` note in the patch records the rationale.

Then **delete** the band-aid: `chart/templates/middleware.yaml` and the
`traefik.ingress.kubernetes.io/router.middlewares` annotation in
`chart/templates/ingress.yaml`.

### Component 2 — Slash-tolerant services + gateway hygiene (Bug B)

**Primary fix — eliminate the redirect at the source, centrally in `service-kit`.**
`make_service_app` builds every service's FastAPI app, so the fix lands once and
all five services inherit it:

- Construct the app with `redirect_slashes=False` (no 307s ever).
- Add a small, **route-aware** path-normalization mechanism so both `/x` and `/x/`
  resolve to the same handler **in-process** (no redirect). The implementation
  (pinned in the plan via TDD) consults the app's route table to pick the variant
  that matches, rewriting the ASGI `scope["path"]` before routing. It must NOT
  blindly add/strip slashes (e.g. `/api/chunks/{id}/submit` must be untouched).

This makes the services immune to Dapr's trailing-slash normalization regardless
of which client calls them.

**Defense-in-depth — gateway `Location` hygiene.** In the gateway proxy
(`components/services/gateway`), rewrite any upstream 3xx `Location` response
header that points at an internal upstream base into a relative path before
streaming it back, so no internal address can ever leak even if some future
upstream emits an absolute redirect. (Standard reverse-proxy behavior, à la nginx
`proxy_redirect`.)

### Component 3 — Permanent Playwright e2e regression guard

Promote the throwaway smoke script into a maintained suite:

- Add `@playwright/test` (dev dep, bun-managed) and a `playwright.config.ts` with
  a configurable `baseURL` (env `RASK_E2E_BASE_URL`, default `http://localhost`).
- Specs: for every MFE route (catch-all `/`, each `/default/<domain>` and its real
  index sub-routes), assert (a) HTTP 200, (b) **no failed `_app/*` requests** and
  **no page errors** (proves hydration), and (c) at least one real `/api/*`
  round-trip returns 2xx (proves the gateway path end-to-end). Capture a
  screenshot per route as an artifact.
- A `make e2e` / package script target. Document it as the post-deploy gate in
  `deployment.md`. Not wired into the default unit `make check` (needs a running
  cluster); it runs against a live deploy.

## Testing strategy (TDD)

- **Component 1:** a build-output assertion (patched `handler.js` contains
  `client\`` not `client${base}\``) + the e2e suite proving assets 200 + hydration.
- **Component 2:** unit tests in `service-kit` — both `/x` and `/x/` hit the same
  handler returning 200, no 307; a non-collection path with an `{id}` segment is
  not mangled. Gateway unit test — an upstream 3xx with an absolute internal
  `Location` is rewritten to a relative path.
- **Component 3:** the suite itself is the regression test; it must go red on the
  pre-fix images and green after.

## Rollout / verification

1. Land Components 1–2 with unit tests green (`pytest`, `ruff`, `ty`,
   `svelte-check`, `helm lint/template`).
2. `make k3s-build && make k3s-import && make k3s-up` (now unattended via the
   scoped sudoers for `k3s ctr images import`).
3. Run `make e2e` against the live deploy → all routes green (hydration + API
   round-trip), the Traefik middleware gone.

## Risks

- **`bun patch` + isolated linker:** verify the committed patch applies cleanly
  under `bun install --frozen-lockfile` inside the docker builder. Mitigation: the
  plan's first task validates the patch in the image build before touching the
  chart.
- **Path-normalization over-reach:** a naive slash rewrite could corrupt
  legitimate paths. Mitigation: route-aware implementation + explicit negative
  tests.
- **SSR origin for remote `query()`:** server-side `getRequestEvent().fetch` must
  resolve `/api/*` to the gateway through the ingress; confirm the chart's
  `x-forwarded-*` / origin config makes the SSR fetch reach the gateway. The e2e
  API-round-trip assertion catches a regression here.
