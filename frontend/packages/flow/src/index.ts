/**
 * The estate's Svelte Flow layer — three species of canvas, one binding.
 *
 * - `GraphCanvas` — the interactive read-only VIEWER (Background, Controls, optional MiniMap,
 *   signature-based auto-refit). Five components across three zones used to mount this chrome by hand
 *   and drifted; the visible cost was `FlowAutoFit` existing twice with different bodies.
 * - `StaticFlow` — the SSR renderer: explicit dims and handle positions so it renders with no browser
 *   layout engine at all (media's `/diagram` static-HTML endpoint). Deliberately NOT the viewer with
 *   props off — the two have incompatible contracts (client hooks vs server render).
 * - `layout` — the layered barycentre placer. Generic coordinates math that was misfiled under
 *   `panels/lineage` while the FGA access graph imported it cross-domain.
 * - `FlowAutoFit` — the one surviving copy (`maxZoom: 1`).
 *
 * `./styles.css` is the vendor sheet's `--xy-*` theme mapped onto the OKLCH tokens — imported per zone
 * in `app.css` under `layer(base)`, immediately after `@xyflow/svelte/dist/style.css`, exactly as
 * `@rask/dockview/styles.css` is. It lives here and not in `@rask/ui` for the same reason dockview's
 * does not: the design system carries no vendor-canvas bindings.
 *
 * What does NOT belong here: domain graphs. `LineageGraph` is a lineage panel (lakehouse's own lib), the FGA
 * builders are lakehouse access logic, the workflow EDITOR is media's. The binding stays domain-free.
 */
export { default as GraphCanvas } from './GraphCanvas.svelte';
export { default as StaticFlow } from './StaticFlow.svelte';
export { default as FlowAutoFit } from './FlowAutoFit.svelte';
export { depths, layout } from './layout';
export type { LayoutEdge, Placed } from './layout';
