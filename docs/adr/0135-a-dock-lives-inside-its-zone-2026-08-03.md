# 0135. A dock lives inside its zone (2026-08-03)

Source: `docs/architecture/global-workbench.md:3-7,116-161` (at `44b354f3`) ("The standing decision (final, 2026-08-03 evening)", plus
the panel rule added 2026-08-04). That header named `/explorer/workbench` "the estate's ONE dock"; the standing
decision and the code say three (see Consequences).

## Context

Two designs for a multi-panel workbench were built, shipped and reversed on the same day:

- a `@rask/panels` package that moved the lineage and compute panels out of their zones, plus a `workbench` zone
  importing all of it (build-time composition). It hollowed the zones — the panels ARE the zones' domain code — broke
  the rule that `frontend/packages/*` are libraries, and lost the zones' live stores (compute's panels took one-shot
  snapshots, and 106 lines of `compute.remote.ts` were duplicated because remote functions are per-app);
- a cross-zone compositor zone mounting each zone's panels as custom elements (runtime composition). A custom element
  cannot import a remote function and cannot reuse a component bound to `$app/*` or the zone's live tick, so most
  panels had to be MIRRORED, re-implemented to look like the zone page. Its one unique capability, mixing panels from
  different zones, was a workflow nobody had; every complaint it drew was about panel quality.

## Decision

- **A dock lives INSIDE its zone.** Not a compositor, not custom elements. A dock panel is the zone's real component,
  importing the zone's own remote functions and sharing one store through `createContext`.
- **One dock per zone that earns one, at zone level** (`/<zone>/workbench`, never inside an area). A dock is earned,
  never granted by symmetry: it costs about 100 KB deferred and pays only where a multi-panel view of ONE subject is
  the actual workflow.
- **A panel renders the PAGE's component** (2026-08-04): the view lives in one component the route and the panel both
  render; where the two read different sources, rows arrive as a prop so the dock keeps its shared store.
- **Extract the mechanism, keep the domain.** `@rask/dockview` and `@rask/flow` stay as libraries; a panel's domain
  code stays in its zone. Iframes stay rejected for first-party panels (they are the untrusted-code tool).

## Consequences

- Three docks today: `/compute/workbench`, `/explorer/workbench`, `/lakehouse/workbench`
  (`frontend/microfrontends/{compute,explorer,lakehouse}/src/routes/workbench`), pinned EXACTLY by
  `frontend/packages/zone-contract/src/dock-reachability.test.ts:90-92`, so neither a compositor, a symmetry dock nor
  a nested path can return unnoticed. Home, studio, models and the annotator carry none.
- The `workbench` zone, both zones' `src/lib/elements/**`, the element builds and budgets, `@rask/dockview/contract`
  and the element-only `/api/audit` shim were deleted; `@rask/dockview` (its chrome, named views, `ViewSidebar`) and
  the catalog's `dock-layout` / `dock-layout-library` user-state envelopes were kept and serve the in-zone docks.
- The zone is the unit of ownership: moving a zone's panels into a shared package hollows it, and moving them behind a
  cross-zone element boundary starves them.
