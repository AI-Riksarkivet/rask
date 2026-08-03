# open_ia — the estate has two levels, and the navbar has to say which one you are on

The IA round of 2026-08-03 established that a **project is the TOP of the hierarchy** — project ›
warehouse › namespace › table — and moved the projects surface into the main menu. This round
finishes the shape that ruling implies: **the estate root and the inside of a project are two
different places, and the top navbar is what tells them apart.**

## The two levels

| Level | Where | Top navbar |
| --- | --- | --- |
| **Main menu** — the estate | `/`, `/projects`, `/projects/<id>`… wait, no — see below | `Home` · `Projects` · `Settings` |
| **Inside a project** | any zone route, with a project active | `Lakehouse` · `Compute` ⟵ gap ⟶ `Search` · `Annotate` · `Train` · `Studio` |

**Scoping is by CONTEXT, not by URL** (ruled 2026-08-03). Entering a project sets the active project;
zone URLs stay `/lakehouse/…`, `/compute/…`. The alternative — prefixing every zone path with
`/projects/<id>/` — would rewrite eight zones' `paths.base`, the ingress rules, the microfrontend
proxy keys, the deploy-path gate and ~50 cross-zone links, for a hierarchy the switcher already
expresses. Rejected on cost, not on taste; revisit only if two projects must be open side by side.

`projectFromHost` (`shell/breadcrumb.ts`) already parses a project from the request host and stays as
it is — a host-scoped deployment simply arrives with the context pre-set.

## What each surface is

**`/` — Home.** An insights landing: what is happening in the estate. Today `/` is the project
GALLERY, which is why a user with no memberships currently gets *"You are not a member of any project
yet"* as their entire product. That message is a projects-list empty state and belongs on
`/projects`. Home may be a scaffold for now — badge it, as `train` does — but it must be a real route
with a real empty state, not a redirect.

**`/projects` — the list.** Gallery by default with a **table toggle**, persisted per user in the
existing `dock-layout` user-state document (the estate's only per-subject store). Create lives here.
The membership empty state moves here from `/`.

**`/projects/<id>` — one project's metadata and overview.** Exists today (ported from the lakehouse).
This is the page the zone bar appears on, because opening a project is what puts you inside one.

**`/settings` — estate configuration.** Currently the `Settings` navbar entry points straight at
`/lakehouse/governance/access`, which is a placeholder standing in for a page that does not exist. It
becomes a real home-zone route holding: notifications, defaults applied to a NEW project, and
auth/authz (the governance rows that already moved there). Governance routes are still SERVED by the
lakehouse app — moving them behind a `/settings` base is a separate change and needs redirects.

## Primary vs secondary, visibly

`Lakehouse` and `Compute` are `tier: 'primary'` in `nav-config.ts` today and render one step louder.
The ruling adds a **gap**: primary entries sit together, then a spacer, then the task destinations.
The tier data already exists; only the rendering is missing.

## Open questions this does NOT answer

- What Home's insights actually are. A scaffold badge is honest; inventing metrics is not.
- Whether `/settings` should eventually OWN the governance routes rather than link to the lakehouse's.
- `zoneOf('/projects') === 'projects'`, so the navbar's Projects link still costs a document load
  inside home. Fixing it touches every zone's link behaviour.

## Goal (paste when ready)

> **/goal** The estate reads as two levels and the navbar proves it. (1) `/` is HOME — an insights
> landing with an honest scaffold badge, NOT the project gallery — and the "not a member of any
> project yet" empty state has moved to `/projects`. (2) The main-menu bar is exactly `Home ·
> Projects · Settings`; the in-project bar is exactly `Lakehouse · Compute` then a visible gap then
> `Search · Annotate · Train · Studio`, driven by the existing `tier` data, and which bar renders is
> decided by whether a project is ACTIVE (context, not URL — zone paths are unchanged). (3)
> `/projects` offers BOTH a gallery and a table view, toggled and persisted per user in the
> `dock-layout` user-state document, with create and the membership empty state on it. (4)
> `/projects/<id>` shows the project's metadata and is a place the zone bar appears. (5) `/settings`
> is a REAL home-zone route carrying notifications, new-project defaults, and auth/authz — the
> `Settings` entry stops pointing at `/lakehouse/governance/access`. Prove each clause in-transcript:
> `home` and `lakehouse` e2e green with NEW tests pinning each bar's exact entries at both levels and
> the toggle's persistence, `@rask/ui` + `@rask/zone-contract` green, `bunx turbo --cwd=frontend run
> check lint` 0 errors on every touched package, the svelte MCP autofixer reporting `issues=[]` on
> every touched `.svelte`, and the zone images rebuilt and rolled out so the running cluster serves
> it. Constraints: concurrent sessions are active — commit own paths only; no zone's `paths.base`,
> ingress rule or proxy key changes; fail-closed stays fail-closed (Settings is estate-admin only and
> ABSENT otherwise); don't invent Home's metrics — scaffold and badge it. Any deferred clause is
> named in the final report with its reason.

---

Delete this file when the round lands.
