# frontend — the rask console (bun workspace)

The console is ONE SvelteKit 2 + Svelte 5 single-page app with no backend of its own. The browser
signs in to the platform's issuer (Dex in development) with OpenID Connect authorization code + PKCE as
the public client `rask-web`, keeps the tokens in memory, and calls the lakehouse's doors and the
notifications inbox with its bearer, same-origin, through the platform's Gateway. The built files are
served by `console/serve.ts`.

```
frontend/
  package.json      # workspace root (bun workspaces: console, packages/*); plain bun scripts
  TOOLING.md        # the toolchain: one linter, one formatter, the rules that are off and why
  .oxlintrc.json    # oxlint — every extension, .svelte included (via @rsvelte/oxlint-plugin)
  .oxfmtrc.json     # rsvelte-fmt + oxfmt — every extension
  patches/          # bun patchedDependencies (bits-ui, apache-arrow — see TOOLING.md)
  console/          # the app
    src/routes/     #   /  /projects  /projects/[project]  /catalog/...  /admin/...  /settings/...
                    #   /preferences/...  /notifications  /workbench  /auth/{login,callback,logout}
    src/lib/auth.svelte.ts    # sign-in (oidc-client-ts), /config.json, the bearer provider
    src/lib/remote.svelte.ts  # query / query.live / command — the reads and writes, in the browser
    src/lib/**/*.api.ts       # each surface's reads and writes against its upstream
    src/lib/upstreams.ts      # where each upstream answers: /api/catalog, /api/lineage, /api/notifications
    serve.ts        #   the static server the image runs (SPA fallback, /config.json, the CSP header)
    e2e/            #   the Playwright suite (see below)
  packages/
    ui/             # @rask/ui — the design system (shadcn-svelte on bits-ui 2), AppShell + nav-config,
                    #   Storybook. The only package with a build step (svelte-package → dist/).
    api/            # @rask/api — the browser transport (`client`), the /management/v1/me contract, the
                    #   inbox, lineage and dock-layout clients, the generated OpenAPI types
    dockview/       # @rask/dockview — the Svelte binding over dockview 7 (the workbench)
    flow/           # @rask/flow — the Svelte Flow binding (the governance graphs)
```

## Runtime config and security

`/config.json` is `{issuer, clientId, scopes}` (extra keys are ignored), rendered by the console's
release and named to `serve.ts` by `CONSOLE_CONFIG_FILE`; the built app carries no environment. With no
config, sign-in is off and no bearer is sent. `issuer` may be absolute or a same-origin path.

The content security policy is SvelteKit's (`kit.csp`, mode `hash`) and travels with the build.
`serve.ts` sends it as a header, adding the issuer's origin to `connect-src` and `frame-ancestors 'none'`,
with `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

## Scripts (run from the repo root as `bun --cwd=frontend run <script>`)

| Script            | What it runs                                                                  |
| ----------------- | ----------------------------------------------------------------------------- |
| `dev`             | builds `@rask/ui`, then the console's `vite dev` on :5173, `/api` → `RASK_EDGE_URL` (default `http://localhost:8888`) |
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
lineage, the inbox, the NATS monitor). Sign-in runs the real PKCE path against `e2e/mock-oidc.ts`, a mock
issuer served same-origin at `/oidc`; a spec chooses who signs in with `signIn()` (the issuer reads the
`mock_oidc_identity` cookie), and every API call then carries that bearer. Three origins, one per family
of specs, each with its own mocks: auth-off, the estate pages, and the lakehouse pages (ports in
`e2e/ports.ts` and `e2e/admin/ports.ts`).
