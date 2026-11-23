/**
 * The estate's Svelte Flow layer — three species of canvas, one binding.
 *
 * - `GraphCanvas` — the interactive read-only VIEWER (Background, Controls, optional MiniMap,
 *   signature-based auto-refit). Five components across three pages used to mount this chrome by hand
 *   and drifted; the visible cost was `FlowAutoFit` existing twice with different bodies.
 * - `StaticFlow` — the static renderer: explicit dims and handle positions, so it is drawn whole at
 *   its first paint with nothing measured (the project hierarchy draws with it). Deliberately NOT
 *   `GraphCanvas` with props off: that one's hooks need a live canvas, and this has none.
 * - `layout` — the SYNCHRONOUS layered barycentre placer. Generic coordinates math that was misfiled
 *   under `panels/lineage` while the FGA access graph imported it cross-domain. It implements the
 *   first three phases of Sugiyama and stops before coordinate assignment.
 * - `FlowAutoFit` — the one surviving copy (`maxZoom: 1`).
 *
 * `./styles.css` is the vendor sheet's `--xy-*` theme mapped onto the OKLCH tokens — imported per page
 * in `app.css` under `layer(base)`, immediately after `@xyflow/svelte/dist/style.css`. It lives here
 * and not in `@riksarkivet/ui` because the design system carries no vendor-canvas bindings.
 *
 * Everything this module exports is domain-free: it knows nodes, edges and boxes, never a catalog object.
 * The FGA graph builders stay with the access pages (`#lib/access`). The one domain view kept in this
 * folder, `./hierarchy` (project › warehouse › namespace › table), is its own module: it draws on
 * `StaticFlow`, two pages render it, and nothing here imports it.
 */
export { default as GraphCanvas } from './GraphCanvas.svelte';
export { default as StaticFlow } from './StaticFlow.svelte';
export { default as FlowAutoFit } from './FlowAutoFit.svelte';
export { layout } from './layout';
export type { LayoutEdge, Placed } from './layout';
