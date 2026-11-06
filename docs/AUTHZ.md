# Who can see and do what in the console

The estate has **two** identity facts, both from the frozen `GET /management/v1/me` contract
(read through `@rask/api`), both derived from FGA by the catalog:

| Fact                         | Type                       | Means                                                                 |
| ---------------------------- | -------------------------- | --------------------------------------------------------------------- |
| `estate_admin`               | `boolean`                  | `can_observe_events` on the FGA root object — the whole-estate tier.    |
| `projects[].role`            | `'admin' \| 'member'`      | Per-project membership, one row per project the caller can see.        |

There is no third tier and no client-side role invention: anything finer (can this user drop THIS
table, grant on THIS namespace) is an FGA check the backend makes per request. The frontend never
decides authorization — it decides **disclosure**.

## The three layers, and what each one is for

1. **Information architecture** — the navbar and sidebar do not render a link to a surface the
   identity cannot use. `estate_admin` is the only fact the nav reads
   (`packages/ui/src/lib/shell/nav-config.ts`). This is courtesy, not security.
2. **The route guard** — the console's admin layout reads `/management/v1/me` in the browser and shows a
   `403` page to a non-`estate_admin` before any admin component renders. Fail-closed on every ambiguity: no
   token, a 401/403, a timeout, an unreachable catalog, or a `/management/v1/me` response that drifted from the
   contract all land in the same branch as "not an admin". The console has no server of its own, so this guard runs
   in the browser and is **disclosure, not authorization**: a person who edits the page can render the admin views,
   and every call those views make is still refused by the catalog.
3. **The backend** — every read and write is a browser call to the catalog, lineage or the inbox with the signed-in
   person's bearer, and each runs the real FGA check against that bearer. **This is the only layer that cannot be
   bypassed**, and it is the one that actually authorizes.

## What the console holds

The console is a single-page app with no backend of its own, as Lakekeeper's console is:

- **One credential, the person's own.** The browser signs in to the platform's issuer (Dex in development) with
  OIDC and PKCE on its own public client (`rask-console` in development, declared in `deploy/fixtures/console/dex-values.yaml`)
  and attaches that person's access token to every call. The development Dex binds every authorization code to an S256
  challenge (`oauth2.pkce.enforce`), and its own sign-in client `rask-web` is confidential, with no redirect, so no
  browser signs in through it. There is no service credential anywhere in the console, so no anonymous or signed-out
  read reaches a door with anyone's authority.
- **Sign-out ends the issuer's session where the issuer offers it.** When discovery advertises an
  `end_session_endpoint`, the console redirects there with the held id token as `id_token_hint`, which also drops the
  user; otherwise it drops the user and sends the next sign-in with `prompt=login`, so an issuer that keeps a session
  authenticates the person again rather than reusing it (`logout` in `frontend/console/src/lib/auth.svelte.ts`). The
  dev Dex advertises no end-session endpoint and keeps no session: Dex v2.46.1 does both, and reads `prompt`, only
  with its sessions flag on (`DEX_SESSIONS_ENABLED`, off by default, unset in the fixture), so it shows its login form
  on every sign-in.
- **Tokens live in memory.** Nothing is written to `localStorage` or a cookie; only the transient PKCE state survives
  the sign-in redirect. A reload signs in again through the issuer: at once from an issuer that keeps a session,
  through the login form of the dev Dex, which keeps none. A strict Content Security Policy,
  set by the console's own server, limits where the page may load code from and connect to.
- **Same origin, no CORS.** The console's release attaches an HTTPRoute to the platform's Gateway beside the
  lakehouse's, so the page and the APIs share one origin.
- **Signed-out page loads start the sign-in**, and a call answered `401` does the same, so an expired session never
  renders a half-loaded page as if it were empty.

## Per surface

| Surface               | Anonymous (auth on)      | Signed in, no estate_admin                                     | estate_admin                     |
| --------------------- | ------------------------ | -------------------------------------------------------------- | -------------------------------- |
| `/`, `/projects`      | Sent to sign in.         | Landing + the project gallery, one card per `projects[]` row.    | Same, plus every project.        |
| `/catalog/…`          | Sent to sign in.         | Catalog, lineage and governance views. Every read and write is FGA-checked per object by the catalog — the danger-zone controls RENDER, and the backend refuses the ones this identity cannot do. | Same; FGA still decides per object. |
| `/admin/…`            | Sent to sign in.         | **403 page from the route guard.** No admin nav entries; every admin call is refused by the catalog. | Full access.                     |
| `/notifications`      | Sent to sign in.         | The person's own inbox, from the notifications inbox.            | Same.                            |

## Known and deliberate

- **The data plane's destructive controls are not hidden by role.** Drop, deregister, grant and revoke
  render for any signed-in user; the catalog's owner-tier gate (`can_drop`) refuses the call. That is
  authorization-correct and disclosure-loose: the button tells you the operation exists. Hiding it
  would need a per-object capability in `/management/v1/me`, which the contract does not carry — adding one is a
  backend change, not a frontend one.
- **`projects[].role` drives display only.** The gallery and the projects list label a membership
  `admin` or `member`; nothing in the UI gates on it. Per-project authorization is FGA's, per request.
- **Auth-off stacks are fully open** by construction — no OIDC env means no identities, and the
  backend answers `estate_admin: true` for dev parity.

## What pins this

The console's Playwright suite (`frontend/console/e2e`) drives the admin guard: a member sees the `403` page on every
admin route, an admin passes, and a catalog outage fails closed. That the catalog refuses an admin call from a
non-admin bearer is pinned by the catalog's own suites, because that refusal is the one that authorizes.
