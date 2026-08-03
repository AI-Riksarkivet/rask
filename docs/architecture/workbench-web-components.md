# Cross-zone workbench via custom elements — the runtime-composition plan

*Status: **proposed, spike-first, not started**. 2026-08-03. Successor to the reversed build-time
design in `global-workbench.md`. Nothing here is committed until the spike's exit criteria pass.*

## The idea in one paragraph

Each zone keeps its panels — code, stores, fetchers, deploys — and *additionally* publishes chosen
panels as **custom elements** (`rask-compute-jobs`, `rask-lakehouse-lineage-graph`, …), built by that
zone's own Vite as a small library bundle and served from that zone's own deployment. A compositor
page (a route, not a zone that owns panel code) loads each element's script from the owning zone and
mounts it in a `@rask/dockview` dock. Zones stay owned and independently deployable; the compositor
composes *running* zone code instead of absorbing source. This is the micro-frontends skill's own
prescription for fine-grained in-page composition: "favor native browser features (custom elements,
CustomEvent)", tags "namespaced with a team prefix".

## Why custom elements and not the alternatives

| Option | Who uses it | Why (not) here |
|---|---|---|
| **Custom elements** | Home Assistant custom cards; SAP Luigi; Salesforce LWC | First-party trusted code, one framework, needs the host's CSS cascade → the fit |
| iframes | VS Code webviews, Grafana plugins | The **untrusted**-code tool: process isolation we don't need, at the cost of the style cascade, focus/keyboard seams, and postMessage-only state. (The dockview fork's never-re-parent guarantee makes iframes *viable* — panels move without reload — so this stays the answer if a third-party plugin surface ever appears.) |
| Module federation | webpack/rspack shops | Unavailable: rask builds on Vite 8 + rolldown |
| Import-map ESM sharing | — | Rejected: silent dual-runtime failures when zone builds drift |
| Build-time package | the reversed `@rask/panels` | Hollows the zones; couples releases; kills live stores + per-app remote functions |

## The verified Svelte 5 pattern (checked against the installed compiler, 5.56.8)

- A **thin wrapper** `.svelte` per exported panel — the panel component itself stays a normal
  Svelte component usable in-zone:

  ```svelte
  <svelte:options customElement={{ shadow: 'none' }} />
  <script lang="ts">
  	import JobsPanel from '$lib/dock/panels/JobsPanel.svelte';
  	let { alert } = $props();
  </script>

  <JobsPanel {alert} />
  ```

  `shadow: 'none'` is reachable **only** through the object form of `<svelte:options customElement>`
  (not the `customElement: true` compiler flag), and only wrapper files are compiled with
  `customElement: true` (a Vite `include` on `src/lib/elements/*.svelte`).
- **No `tag` in the options** — registration is a manual, guarded
  `if (!customElements.get(name)) customElements.define(name, Element)` in the entry, so two
  compositor loads (or HMR) never hit the "already defined" throw.
- **Light DOM is load-bearing, not a preference** (rask-styling): Tailwind 4 utilities and the token
  cascade (`@rask/ui/styles/tokens.css`, `layer(base)`) style the panel only if the page's
  stylesheets reach it. Shadow DOM would orphan every class. The compositor page imports the same
  app.css shape every zone does; the elements inherit.
- **Naming**: `rask-<zone>-<panel>`, e.g. `rask-compute-jobs`. The zone prefix is the ownership
  label and the collision guard.

## What each zone adds (per exported panel — nothing moves)

1. `src/lib/elements/<panel>-element.svelte` — the wrapper above.
2. A Vite **library entry** (`build.lib`, one `elements.ts` that imports wrappers + defines tags)
   emitting `static/elements/<zone>-elements.js` into the zone's existing build — served by the
   zone's own Bun server at `/<zone>/elements/…`. No new deployable, no new image.
3. The zone's remote functions / BFF fetchers keep working **because the code still runs from the
   zone's own bundle** — the two walls that broke build-time sharing (per-app remote endpoints, the
   context seam) are not crossed.

## State and events across panels

Inside one zone's dock nothing changes (shared store via context). Between *foreign* elements on the
compositor, context does not cross — by design. The contract is the platform's:

- **Properties in**: the compositor sets typed properties (Svelte CE attribute/prop bridge) for
  initial filter/config.
- **`CustomEvent` out**: panels dispatch namespaced events (`rask:select`, detail typed in
  `@rask/dockview`), the compositor relays to interested siblings. This is the *price* of runtime
  composition and it is bounded: the Treemap→Topics filtering already works this way in-zone and is
  the spike's cross-panel proof case.

## Dock integration notes (from the fork research)

- `defaultRenderer: 'always'` + the fork's stable-parent architecture: element panels attach once,
  moves are style writes — no disconnect/reconnect storm on drag.
- Both `fromJSON` call sites (persistence load + view switch) must pass
  `{ reuseExistingPanels: true }` or a view switch recreates panels (fork feature; already proven
  necessary for iframes, same reasoning for CEs with internal state).
- **Popout disabled** for foreign panels: popout re-parents into another document, which for a CE
  re-runs the full disconnect/connect lifecycle against a window whose stylesheets differ.

## The spike (do this before believing any of it)

**One panel end-to-end**: compute's Jobs panel as `rask-compute-jobs`, loaded by a compositor route
(inside an existing zone — no new zone until the spike passes), mounted in a dock next to a native
panel.

Exit criteria — all observed in a real browser, not asserted from SSR:

1. The element renders **styled** (tokens + Tailwind reach it — the light-DOM claim proven).
2. It shows **live** data through compute's own remote functions while served on the compositor.
3. Dragging the panel around the dock does not remount it (state survives a move).
4. A `CustomEvent` from it reaches a sibling panel via the compositor relay.
5. The compute zone redeploys **alone** and the compositor picks up the new element without its own
   rebuild.
6. Budget: the compositor pays only the element script's bytes; compute's budget.json unchanged or
   raised with the measured number in the commit.

If 1 or 3 fails, stop and write down why before touching anything else — those two are the
architecture, the rest is plumbing.

## Paste-ready goal block

```
GOAL: prove cross-zone runtime composition with ONE custom element, spike-first.
- rask-compute-jobs: wrapper <svelte:options customElement={{ shadow: 'none' }} /> (object form, no
  tag), compiled via a Vite lib entry in the COMPUTE zone, emitted into compute's own build output,
  served by compute's Bun server. Guarded manual customElements.define. No panel source moves.
- A compositor route mounts it in a @rask/dockview dock beside one native panel; both fromJSON call
  sites pass { reuseExistingPanels: true }; popout disabled for the foreign panel.
CONDITIONS (each proven by pasted terminal/browser output, not summary):
1. Playwright screenshot: the element renders STYLED on the compositor (tokens/Tailwind cascade in —
   light DOM proven) with LIVE jobs data via compute's own remote functions.
2. Playwright: drag the element's panel to another group; assert no remount (internal state marker
   survives).
3. Playwright: a CustomEvent dispatched from the element updates a sibling native panel.
4. Rebuild + redeploy compute alone (dagger call zone-image --zone=compute); compositor serves the
   new element with no compositor rebuild — pasted image digest + browser proof.
5. bun --cwd=frontend run lint fmt:check check test green estate-wide, uncached (--force); budgets
   unchanged or raised with the measured number in the commit message.
6. The four dock invariants hold (panels mount ONCE, defaultRenderer 'always', layout per-subject
   never localStorage, dock dynamically imported); no GSAP on dock chrome.
STOP RULE: if styling (1) or move-without-remount (2) fails, halt and record why in
docs/architecture/workbench-web-components.md instead of working around it.
```
