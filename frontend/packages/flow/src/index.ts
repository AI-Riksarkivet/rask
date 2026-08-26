/**
 * The estate's Svelte Flow layer — three species of canvas, one binding.
 *
 * - `GraphCanvas` — the interactive read-only VIEWER (Background, Controls, optional MiniMap,
 *   signature-based auto-refit). Five components across three zones used to mount this chrome by hand
 *   and drifted; the visible cost was `FlowAutoFit` existing twice with different bodies.
 * - `StaticFlow` — the SSR renderer: explicit dims and handle positions so it renders with no browser
 *   layout engine at all (media's `/diagram` static-HTML endpoint). Deliberately NOT the viewer with
 *   props off — the two have incompatible contracts (client hooks vs server render).
 * - `layout` — the SYNCHRONOUS layered barycentre placer. Generic coordinates math that was misfiled
 *   under `panels/lineage` while the FGA access graph imported it cross-domain. It implements the
 *   first three phases of Sugiyama and stops before coordinate assignment.
 * - `resolveCollisions` — the post-render repair pass. A layout engine reserves the box it is TOLD,
 *   and a content-height card is not knowable before it renders; this separates whatever still
 *   overlaps, using the boxes Svelte Flow actually measured. Adapted from Svelte Flow's own
 *   `layout/node-collisions` example.
 * - `elkLayout` — the ASYNC upgrade over elkjs, which does the phase `layout` skips (plus dummy-node
 *   edge routing and real component packing). Same algorithm family Marquez uses for the same
 *   picture. Both ship: a caller that must place inside one tick still needs the sync one.
 * - `ElbowEdge` — draws the route `elkLayout` returned. Without it the routing phase is computed and
 *   discarded: `smoothstep` re-derives a shape from two endpoints, so an edge ELK steered AROUND a
 *   node is drawn straight through it. Falls back to `smoothstep` once an endpoint has moved, which
 *   is what keeps a dragged node's edges honest.
 * - `FlowAutoFit` — the one surviving copy (`maxZoom: 1`).
 * - `FlowCenterOn` — bring ONE node into view. `fitView` frames the whole graph, which is the wrong
 *   answer to "where is the node you just selected" on a canvas big enough for the question to
 *   arise. Nonce-driven, because centring is a gesture and may be repeated on the same node.
 *
 * `./styles.css` is the vendor sheet's `--xy-*` theme mapped onto the OKLCH tokens — imported per zone
 * in `app.css` under `layer(base)`, immediately after `@xyflow/svelte/dist/style.css`, exactly as
 * `@rask/dockview/styles.css` is. It lives here and not in `@rask/ui` for the same reason dockview's
 * does not: the design system carries no vendor-canvas bindings.
 *
 * What does NOT belong here: domain graphs. `LineageGraph` is a lineage panel (lakehouse's own lib), the FGA
 * builders are lakehouse access logic, the workflow EDITOR is media's. The binding stays domain-free.
 *
 * One domain graph is not in a zone either, and it is not an exception to that rule: `@rask/ui/hierarchy`
 * (project › warehouse › namespace › table) draws on `StaticFlow` from the DESIGN SYSTEM, because two zones
 * render it and zones may not import each other. The direction is what matters — @rask/ui depends on this
 * package, never the reverse — so the binding is still domain-free and the stylesheet still ships from here.
 * That subpath is @rask/ui's only vendor-canvas consumer; nothing else there pulls @xyflow/svelte.
 */
export { default as GraphCanvas } from './GraphCanvas.svelte';
export { default as StaticFlow } from './StaticFlow.svelte';
export { default as FlowAutoFit } from './FlowAutoFit.svelte';
export { default as FlowCenterOn } from './FlowCenterOn.svelte';
export { default as ElbowEdge } from './ElbowEdge.svelte';
export { depths, layout } from './layout';
export { elkLayout, routeKey } from './elk-layout';
export type { ElkLayoutResult, ElkRoute, PlacedGroup, RoutePoint } from './elk-layout';
export { resolveCollisions } from './resolve-collisions';
export type { LayoutEdge, Placed } from './layout';
export type { ElkLayoutOptions } from './elk-layout';
export type { ResolveCollisionsOptions } from './resolve-collisions';
