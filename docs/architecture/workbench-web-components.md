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

  Verified against the official custom-elements docs (svelte MCP, 2026-08-03), which add three
  wrapper rules: **declare every prop explicitly** (a bare `$props()` spread gives Svelte no list of
  properties to expose on the element); **no prop names starting with `on`** (the CE bridge parses
  them as event-listener attachments); and `shadow: 'none'` forgoes slots (irrelevant — panels take
  data, not slotted content). The docs also confirm the two claims everything rests on: with no
  shadow root, page styles apply (light DOM cascade), and *"DOM moves which temporarily (but
  synchronously) detach the element from the DOM don't lead to unmounting the inner component"* —
  the destroy fires a tick after `disconnectedCallback` and reconnection cancels it.
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
compositor, Svelte context does not cross — by design. The contract is the platform's, and it is
**less lossy than "attributes and strings"**:

- **Properties in — full JS values, not attributes.** A custom element's *properties* are set by
  assignment (`el.selection = {...}`, `el.rows = arrayOfObjects`) and Svelte's CE bridge feeds them
  straight into `$props()`, reactively. Attributes (strings) are only the fallback for HTML-authored
  usage. Consequence: the compositor can own ONE poll loop or one selection store and push rich
  snapshots down into every foreign panel — so "two zones' panels sharing one live data feed" IS
  achievable; what is not shared is memory (the element gets a copy, not the store).
- **`CustomEvent` out**: panels dispatch namespaced events (`rask:select`, `detail` typed) with
  `bubbles: true, composed: true`; light DOM lets them bubble to the dock container, where the
  compositor listens ONCE by delegation and relays — by setting properties on interested siblings.
  Down = properties, up = events; the compositor is the hub, panels never know about each other.
- Cross-filtering is therefore the same shape the media dock already proves in-zone
  (Treemap→Topics): selection event up, filter property down. That is the spike's proof case.

## Dock integration notes (from the fork research)

**Dockview needs no changes to host custom elements.** A dock panel's body is just a DOM subtree;
the compositor registers ONE generic Svelte panel component (`ForeignPanel`) whose job is: dynamic
`import()` of the owning zone's element script → `await customElements.whenDefined(tag)` →
render `<svelte:element this={tag}>` → wire properties/events. All dock-facing glue — the panel
title, `PanelProps.alert`, params, the picker entry — lives in that wrapper, so the dock's four
invariants and the whole G1–G4 chrome (splits, picker, alerts, named views) work identically for
foreign and native panels. The catalogue maps panel id ↔ script URL + tag name.

- `defaultRenderer: 'always'` + the fork's stable-parent architecture: element panels attach once,
  moves are pure style writes — the CE's `disconnectedCallback` never even fires on a drag. (Even
  where dockview does re-parent, Svelte's CE destroy is deferred a microtask and cancelled on
  same-task reconnect — but 'always' means we never rely on that.)
- Both `fromJSON` call sites (persistence load + view switch) must pass
  `{ reuseExistingPanels: true }` or a view switch recreates panels (fork feature; already proven
  necessary for iframes, same reasoning for CEs with internal state).
- **Popout disabled** for foreign panels: popout re-parents into another document, which for a CE
  re-runs the full disconnect/connect lifecycle against a window whose stylesheets differ.

## Performance discipline

- **Lazy + preloaded**: element scripts load on first use (dynamic import in `ForeignPanel`), with
  `<link rel="modulepreload">` hints for panels in the active saved view, so restoring a view does
  not waterfall.
- **Immutable caching**: element bundles get content-hashed filenames served with
  `cache-control: immutable` — a returning user pays zero bytes until the owning zone redeploys.
- **One runtime per ZONE, not per panel**: each zone emits a single `elements.js` carrying all its
  exported panels, so the ~11–15 KB gz Svelte runtime is paid once per zone (~3 zones ≈ 35–45 KB —
  less than one chart library). Deliberately NOT deduplicated via import maps: shared-runtime drift
  across independently deployed zones is the failure mode import-map sharing was rejected for, and
  per-zone isolation is the feature.
- The compositor's own budget counts the dock + shell only; foreign bytes are the zones' budgets.
  The spike's condition 6 pins the measured numbers.

## Contract drift (the one gotcha that needs standing infrastructure)

TypeScript cannot check across the network boundary, so the prop/event contract is enforced twice:

1. **Types + tests**: event names, `detail` shapes and property names live in a tiny shared
   contract module; a `@rask/zone-contract` test pins them so a zone renaming an event turns CI red
   at build time — the same trick the estate uses for every other cross-zone shape.
2. **valibot at the relay**: the compositor validates every inbound `CustomEvent.detail` before
   forwarding (the estate's standard validation layer), so a drifted zone degrades to a logged,
   dropped event instead of corrupting sibling panels.

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
