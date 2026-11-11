# 0202. The console has one navigation, and filters by project in the URL (2026-11-11)

**Status:** accepted. Source: the owner, 2026-11-11, through the question tool: "Remove both" for the console's top
menu and its project switcher, leaving the sidebar as the one navigation.

## Context

Measured 2026-11-11, on the console after ADR 0201:

- **Two navigations said the same thing.** The top bar's one entry, Lakehouse (`topNav()` in `@rask/ui`'s
  `nav-config.ts`), opened a panel that repeated the sidebar's links, and the estate pages (`/`, `/projects`,
  `/settings/**`) rendered a second bar (Home, Projects, Settings) and no sidebar at all. `navigation-menu` in `@rask/ui`
  existed only to draw that panel.
- **The active project filtered nothing.** The project switcher wrote a cookie, `rask_active_project`
  (`console/src/lib/active-project.ts`), read by the root layout (the shell's label and the breadcrumb's first crumb)
  and by the `/catalog` overview alone. Every list (warehouses, namespaces, tables, promotions) read estate-wide
  whatever it said, so the switcher implied a scope the pages did not have.
- **The catalog's doors are estate-wide by grant.** Every list read answers with what the caller's grants allow, across
  every project; no door takes a project as its scope. A switcher could only ever filter, in the browser, rows the
  caller was already given.
- **References**, as reported to the owner with the question (the consoles were not re-read for this record): two
  reference consoles carry a scope switcher because their APIs are scoped by it (each request names the project or
  the metalake it is for), and a third has none.

## Decision

- **One sidebar on every page**: Home and Projects; a Catalog group (Warehouses, Namespaces, Tables, Storage,
  Promotions); an Operations group (Events, Streams, DLQ); and, pinned at the foot, Workbench and Settings. The top bar
  keeps the sidebar's toggle, the breadcrumb, the bell and the account menu. The breadcrumb is the route's own path.
- **A row the caller may not open is shown disabled with the denial's wording**, never hidden, and a disabled row hands
  out none of its children: Settings and the Operations rows, for anyone but an estate admin (`can_observe_events` on
  the FGA root), and for an identity the console could not resolve, the sign-in reason instead.
- **The switcher, the cookie and the `/catalog` overview go.** `/catalog` redirects (307) to `/catalog/tables`: the
  breadcrumb's Catalog crumb and an old bookmark land on a list rather than a 404. What the overview showed that a
  project's page did not, the warehouse serving the project's gold tier, moved to the project's page, which stays the
  project's overview and links each list filtered to it.
- **The four lists gain a project column and a project filter kept in the URL**, `?project=<id>`: a reload and a shared
  link show the same rows, and "all projects" clears the parameter. The select offers the projects the catalog's
  `GET /management/v1/projects` answers the caller (every project to an estate observer, the caller's own to anyone
  else), and the one the URL names when it is not among them. A row's project is read from reads the lists already
  make: a warehouse names its project, a namespace is its top bound namespace's warehouse's, and a table or a held
  promotion is its namespace's. A row with no project (the lakehouse's own `provenance` namespace is bound to no
  warehouse) shows a dash. **The filter is a view, not a boundary**: it narrows rows already granted, and nothing
  about the person is kept in a cookie or browser storage.
- **A filtered list never shows a guess.** While the reads that tell a row's project are out, it shows its loading
  state; when one of them failed, it says the filter cannot be applied, names the read and its answer, offers to clear
  the filter, and shows no rows. A filter that matches nothing says "No tables in project `<id>`", not that the
  catalog is empty. Promotions are read a page at a time, so their filter says it narrows the holds loaded so far.
- **The workbench's tables panel keeps its filter in its own state, not in the URL.** The plan put the filter in the
  URL; on `/workbench` that would take the reader off the workbench to the tables page, so the panel holds the value
  and the list it renders takes the value and an `onchange` from whoever mounts it. The value resets when the dock
  remounts the panel, as the list's search does.
- **`@rask/ui` keeps what a design system holds** (ADR 0201): code with one consumer lives in that consumer. The audit,
  per exported component and per shell file (console importers counted in `frontend/console/src`):

| Component | Users | Verdict | Action |
|---|---|---|---|
| `alert-dialog` | 4 console files | design system | kept |
| `avatar` | `shell/navbar-user` only | shell part | export dropped |
| `badge` | 9 console files | design system | kept |
| `button` | 11 console files | design system | kept |
| `card` | 5 console files | design system | kept |
| `checkbox` | 2 console files | design system | kept |
| `collapsible` | `shell/page-nav` only | shell part | export dropped |
| `data-table` | 8 console files | design system | kept |
| `dialog` | 4 console files | design system | kept |
| `dropdown-menu` | 1 console file (the dock), the shell | design system | kept |
| `gated-action` | 4 console files, `grant-form` | access-control feature, one consumer | moved to `console/src/lib/access/gated-action`; its story deleted |
| `grant-form` | 1 console file, `grants-panel` | access-control feature | moved to `console/src/lib/access/grant-form` |
| `grants-panel` | 2 console files | access-control feature | moved to `console/src/lib/access/grants-panel` |
| `identity` | 5 console files, `grants-panel` | access-control feature | moved to `console/src/lib/access/identity` with its test |
| `input` | 2 console files | design system | kept |
| `navigation-menu` | the top bar only | drew the removed panel | deleted |
| `popover` | 1 console file, the bell | design system | kept |
| `select` | 10 console files | design system | kept |
| `separator` | the shell only | shell part | export dropped |
| `sheet` | 3 console files, the sidebar | design system | kept |
| `sidebar` | the shell only | shell part | export dropped |
| `skeleton` | 1 console file | design system | kept |
| `switch` | its story only | unused | deleted |
| `table` | 1 console file, `data-table` | design system | kept |
| `tooltip` | 1 console file (the moved `gated-action`), the sidebar | design system | kept |
| `shell/app-shell` | the root layout | the frame | exported |
| `shell/app-error` | `+error.svelte` | the error page | exported |
| `shell/nav-config` | the shell, `console/src/lib/nav.ts` | the sidebar's and the bell's types | exported; `Me` and `MeProject` go (the console's `#lib/api/me.ts` is the one `Me`), the route matchers move to `console/src/lib/nav-match.ts` with their tests, and `pageNavLeaves`, which nothing called, is deleted |
| `shell/app-sidebar`, `page-nav`, `navbar-user`, `activity-center`, `breadcrumb` | the frame | shell parts | export dropped |
| `shell/top-navbar`, `project-switcher` | the root layout | the second navigation | deleted |
| `shell/forbidden-page` | nobody | unused | deleted |

## Considered options

| Option | GitOps | Cloud native | Lance-idiomatic | Zero trust | Outcome |
|---|---|---|---|---|---|
| Keep the top menu and the switcher | n/a | meets | n/a | meets | rejected: a second navigation repeating the first, and a scope the doors do not have |
| Keep the switcher, drop the top menu | n/a | meets | n/a | meets | rejected: the cookie would still filter nothing, or filter every page in the browser behind the reader's back |
| **Remove both; filter each list in the URL** | n/a | meets | n/a | meets | **chosen** (owner) |

## Consequences

- **Every page has the same rail**, so a page is found in one place; the estate pages gain the catalog's rows.
- **A filtered list is a link**: `/catalog/tables?project=acme` reads the same for whoever opens it, within their own
  grants.
- **The console stores nothing about which project a person last looked at.** A person who works in one project
  follows the project page's links, or bookmarks a filtered list.
