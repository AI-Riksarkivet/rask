/**
 * The shared Svelte Flow viewer.
 *
 * Extracted because five components across three zones mounted `<SvelteFlow>` with the same chrome and
 * drifted apart — most visibly `FlowAutoFit`, which existed twice with different bodies, so `maxZoom: 1`
 * applied in one canvas and not its neighbour.
 *
 * `GraphCanvas` is for VIEWERS. The workflow editor keeps its own mount: it has drag-to-connect and
 * connection validation, and a shell that served both would be one component with two modes.
 */
export { default as GraphCanvas } from './GraphCanvas.svelte';
export { default as FlowAutoFit } from './FlowAutoFit.svelte';
