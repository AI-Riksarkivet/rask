# Who can see and do what, per zone

The estate has **two** identity facts, both from the frozen `GET /v1/me` contract
(`frontend/packages/api/src/me.ts`), both derived from FGA by the catalog:

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
2. **The route door** — `microfrontends/lakehouse/src/routes/admin/+layout.server.ts` throws
   `403` on the SERVER for a non-`estate_admin`, before any admin component is rendered or sent. The
   root `+layout.svelte` repeats the check client-side so a soft navigation cannot outrun it.
   Fail-closed on every ambiguity: no token, a 401/403, a timeout, an unreachable catalog, or a
   `/v1/me` response that drifted from the contract all land in the same branch as "not an admin".
3. **The backend** — every read and write goes through the zone's BFF to the catalog or the lineage
   plane, which run the real FGA check against the forwarded user bearer. **This is the only layer
   that cannot be bypassed**, and it is the one that actually authorizes.

## What the BFF itself enforces

The BFF is a bearer-forwarding proxy, not a policy engine. Its own rules:

- **Writes are session-only.** Every `POST`/`PUT`/`PATCH`/`DELETE` route refuses with `401` when auth
  is enabled and there is no session, without the request leaving the BFF. The service credential is
  **never** attached to a write — that was a real confused-deputy hole (2026-07-13), and it is why the
  `/api` and `/capi` catch-alls are GET-only and every write is its own narrow, body-enumerated route.
- **The catalog door is OIDC-only.** `makeCatalogProxy` has no service-token fallback at all, so an
  anonymous request to `/capi/**` reaches the catalog with no credential and gets the catalog's honest
  `401`.
- **Reads on the lineage plane have a service-credential fallback.** `makeLineageProxy` attaches
  `LINEAGE_SERVICE_TOKEN` for `GET`/`HEAD` when there is no session, so a governed stack can serve
  lineage reads without a per-user login. That subject (`frontend.serviceIdentity`, default
  `service-web`) must be READER on the warehouse and allowlisted in `LINEAGE_SERVICE_SUBJECTS` — its
  reach is exactly what FGA grants it, and nothing else in the estate has this fallback.
- **Signed-out page loads are redirected**, not 404ed: `isGatedPageRequest` sends a browser `GET` for
  HTML to the home zone's `/auth/login?redirect=…`. API routes are deliberately excluded so they keep
  returning `401` JSON instead of an HTML redirect.

## Per zone

| Zone            | Anonymous (auth on)                     | Signed in, no estate_admin                                     | estate_admin                     |
| --------------- | --------------------------------------- | -------------------------------------------------------------- | -------------------------------- |
| `home` `/`      | Landing + the OIDC round-trip.          | Landing + the project gallery, one card per `projects[]` row.    | Same, plus every project.        |
| `lakehouse/data`| Redirected to login.                    | Catalog, lineage and models UI. Every read and write is FGA-checked per object by the catalog — the danger-zone controls RENDER, and the backend refuses the ones this identity cannot do. | Same; FGA still decides per object. |
| `lakehouse/admin`| Redirected to login.                   | **403 from the server.** No admin HTML, no admin nav entries.    | Full access.                     |
| `media`         | Redirected to login.                    | Search, atlas, workflow. Writes (tag, batch submit) session-only.| Same.                            |
| `annotator`     | Redirected to login.                    | The Pixi canvas, read and write. Annotation writes session-only. | Same.                            |

## Known and deliberate

- **The data plane's destructive controls are not hidden by role.** Drop, deregister, grant and revoke
  render for any signed-in user; the catalog's owner-tier gate (`can_drop`) refuses the call. That is
  authorization-correct and disclosure-loose: the button tells you the operation exists. Hiding it
  would need a per-object capability in `/v1/me`, which the contract does not carry — adding one is a
  backend change, not a frontend one.
- **`projects[].role` drives display only.** The gallery and the projects list label a membership
  `admin` or `member`; nothing in the UI gates on it. Per-project authorization is FGA's, per request.
- **Auth-off stacks are fully open** by construction — no OIDC env means no identities, and the
  backend answers `estate_admin: true` for dev parity. Dev servers and the hermetic e2e run this way.

## What pins this

`microfrontends/lakehouse/e2e/admin/admin-gate.spec.ts` — a member is refused on every admin
route by the server door, an admin passes, a catalog outage fails closed, and a browser that lies about
its own identity (a member's session plus a mocked `/v1/me` claiming `estate_admin`) still gets `403`,
because the door that decides never asked the browser.
