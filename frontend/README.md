# frontend — the rask console (bun workspace)

The console is ONE SvelteKit 3 + Svelte 5 single-page app with no backend of its own. The browser
signs in to the platform's issuer (Dex in development) with OpenID Connect authorization code + PKCE as
its own public client `rask-console`, keeps the tokens in memory, and calls the lakehouse's doors with its
bearer, same-origin, through the platform's Gateway. The built files are served by `console/serve.ts`.
The navbar's bell lists the reader's own writes and their outcomes, kept in the browser for the tab.

```
frontend/
  package.json      # workspace root (bun workspaces: console, packages/*); plain bun scripts
  TOOLING.md        # the toolchain: one linter, one formatter, the rules that are off and why
  .oxlintrc.json    # oxlint — every extension, .svelte included (via @rsvelte/oxlint-plugin)
  .oxfmtrc.json     # rsvelte-fmt + oxfmt — every extension
  patches/          # bun patchedDependencies (bits-ui, apache-arrow — see TOOLING.md)
  console/          # the app
    vite.config.ts  #   the SvelteKit config (`sveltekit({...})`: adapter-static, the CSP), the dev server
    package.json    #   `imports` declares `#lib/*` → `src/lib/*`, imported with the file's extension
    src/routes/     #   /  /projects  /projects/[project]  /catalog/...  /admin/...  /settings/...
                    #   /workbench  /auth/{login,callback,logout}
    src/lib/auth.svelte.ts    # sign-in (oidc-client-ts), /config.json, the bearer provider
    src/lib/reads.svelte.ts   # query / query.live / command — the reads and writes, in the browser;
                              #   `command(label, …)` records each write's outcome in the activity log
    src/lib/activity.svelte.ts # the activity log the bell lists: sessionStorage under the principal,
                              #   100 entries, 24 h, forgotten at sign-out
    src/lib/**/*.api.ts       # each surface's reads and writes against its upstream
    src/lib/upstreams.ts      # where each upstream answers: /api/catalog, /api/lineage
    serve.ts        #   the static server the image runs (SPA fallback, /config.json, the CSP header)
    e2e/            #   the Playwright suite (see below)
  packages/
    ui/             # @rask/ui — the design system (shadcn-svelte on bits-ui 2), AppShell + nav-config
                    #   (the bell is `shell/activity-center.svelte`), Storybook. The only package with a build step (svelte-package → dist/).
    api/            # @rask/api — the browser transport (`client`), the /management/v1/me contract, the
                    #   lineage and dock-layout clients, the generated OpenAPI types
    dockview/       # @rask/dockview — the Svelte binding over dockview 7 (the workbench)
    flow/           # @rask/flow — the Svelte Flow binding (the governance graphs)
```

## Runtime config and security

`/config.json` is `{issuer, clientId, scopes}` (extra keys are ignored), rendered by the product chart
(`chart/templates/console.yaml`) and named to `serve.ts` by `CONSOLE_CONFIG_FILE`; the built app carries
no environment. Sign-in is off only where the config says `{"signIn": false}`; a missing or malformed
config is refused, and the console answers 503 naming why rather than signing nobody in. `issuer` may be absolute or a same-origin
path.

The content security policy is SvelteKit's (the `csp` option of `sveltekit()` in `console/vite.config.ts`,
mode `hash`) and travels with the build.
`serve.ts` sends it as a header, adding the issuer's origin to `connect-src` and `frame-ancestors 'none'`,
with `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. A path with a malformed
percent-escape answers 400.

Signing out sends the browser to the issuer's end-session endpoint with the id token as
`id_token_hint`, so the issuer ends that session too; the in-memory user is held until the redirect is
built. An issuer that advertises no `end_session_endpoint` offers no way to end its session from the
browser, so sign-out drops the tokens and goes to `/auth/login?prompt=login`: an issuer that keeps a
session must authenticate whoever signs in next instead of answering from it. The dev Dex is such an
issuer with no session to reuse: Dex v2.46.1 keeps sessions, reads `prompt` and advertises its
end-session endpoint only with its sessions flag on (`DEX_SESSIONS_ENABLED`, off by default and unset in
the fixture), so it shows its login form on every sign-in.

## Develop against the k3s estate

`make dev-k3s` forwards k3s Traefik's Service to `http://localhost:8080` (or uses whatever already
answers the issuer there), which serves both Dex at `/dex` and every `/api/*` route, and runs
`vite dev` on :5173 with `/api` proxied to it. The dev server serves `console/dev/config.json` as
`/config.json` (issuer `http://localhost:8080/dex`, the public client `rask-console`, the chart's scopes;
`CONSOLE_CONFIG_FILE` names another), and adds that issuer's origin to the page's `connect-src`, which
SvelteKit sends as a header under `vite dev`. Dex must list `http://localhost:5173/auth/callback` as a
redirect URI of `rask-console` and answer CORS for `http://localhost:5173`
(the dev Dex declares both: `console` in `deploy/fixtures/dex/chart/values.yaml`). `make dev` is the same dev server against an edge you
name with `RASK_EDGE_URL` (default `http://localhost:8080`).

## Scripts (run from the repo root as `bun --cwd=frontend run <script>`)

| Script            | What it runs                                                                  |
| ----------------- | ----------------------------------------------------------------------------- |
| `dev`             | builds `@rask/ui`, then the console's `vite dev` on :5173, `/api` → `RASK_EDGE_URL` (default `http://localhost:8080`), `/config.json` from `console/dev/config.json` |
| `build`           | `@rask/ui` (svelte-package), then the console (adapter-static → `console/build`) |
| `check`           | build, then `check` in every package (svelte-check; tsgo for `@rask/api`)     |
| `lint` / `fmt` / `fmt:check` | oxlint / rsvelte-fmt in every package                              |
| `test`            | build, then every package's vitest suite                                      |
| `test:e2e`        | builds `@rask/ui`, then the console's Playwright suite (which builds the app) |
| `knip`            | unused files, exports and dependencies across the workspace                   |
| `storybook` / `build-storybook` | `@rask/ui`'s Storybook                                          |
| `gen:types`       | regenerate `@rask/api`'s catalog and lineage types from `docs/*-openapi.json` |

## The Playwright suite

`console/e2e` runs against the BUILT app served by its own `serve.ts`, behind `e2e/edge.ts`, a Bun
stand-in for the Gateway that routes each `/api/*` prefix to a seed-driven mock upstream (the catalog,
lineage). The mocks serve only doors the real services serve. Sign-in runs the real PKCE path
against `e2e/mock-oidc.ts`, a mock issuer served same-origin at `/oidc`, which holds the exchange to what
Dex enforces for `rask-console`: the authorize request must name that client, its one registered
callback (`<origin>/auth/callback`) and an S256 challenge, the code verifier must hash to that challenge,
and the exchange's client and redirect URI must match the authorize request. A spec chooses who signs in
with `signIn()` (the issuer reads the `mock_oidc_identity` cookie as its session, and honours
`prompt=login` by showing its login page, as an issuer that keeps a session does), and every
API call then carries that bearer. `withoutEndSession()` (`e2e/admin/session.ts`) makes the mock's
discovery advertise no end-session endpoint, as the dev Dex's does not. Three origins, one
per family of specs, each with its own mocks and named by its Playwright project: `auth-off`, `estate`
(the estate pages and the activity log, `e2e/activity/`) and `admin` (`e2e/admin/`: the catalog and operations pages); ports in
`e2e/ports.ts` and `e2e/admin/ports.ts`.

Some pages cannot work, and say so without a request: the audit trail and the streams view (no door
serves the telemetry store or the NATS monitor to a browser), the stores-by-tier view (no door maps
stores to tiers), and remembering a dock layout or the projects view (no door keeps per-person
state).
