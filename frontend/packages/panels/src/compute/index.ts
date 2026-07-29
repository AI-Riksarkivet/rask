/**
 * The compute panels — Ray jobs, cluster capacity and live actors.
 *
 * Their data arrives through the `ComputeReads` context because SvelteKit remote functions are
 * per-app by construction (see `./context.ts`). A zone declares its own `.remote.ts` and provides the
 * three queries; the panels never touch `$app/server`.
 */
export { default as JobsPanel } from './JobsPanel.svelte';
export { default as ClusterPanel } from './ClusterPanel.svelte';
export { default as ActorsPanel } from './ActorsPanel.svelte';
export { getComputeReads, setComputeReads } from './context';
export type { ComputeReads } from './context';
