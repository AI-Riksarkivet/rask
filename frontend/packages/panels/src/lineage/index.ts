/**
 * The lineage panels — graph, runs and events — plus the one store they share.
 *
 * They moved here from the lakehouse zone so the global workbench can mount them: a Svelte component
 * only renders if it is in the bundle, and a zone cannot import from another zone. The lakehouse zone
 * re-imports `LineageGraph` and `LineageState` from here for its own `/lineage` route, which is
 * ordinary package extraction rather than duplication — there is exactly one copy.
 *
 * Everything zone-shaped is injected: `LineageState` takes a lineage CLIENT, and `LineageGraph` takes
 * `base` and `navigate` as props. Nothing in this package imports `$lib/*` or `$app/*`.
 */
export { default as GraphPanel } from './GraphPanel.svelte';
export { default as RunsPanel } from './RunsPanel.svelte';
export { default as EventsPanel } from './EventsPanel.svelte';
export { default as LineageGraph } from './LineageGraph.svelte';
export { LineageState, type LineageClient } from './store.svelte';
export { getLineageState, setLineageState } from './lineage-context';
export { default as FlowAutoFit } from './FlowAutoFit.svelte';
export { depths, layout } from './layout.js';
