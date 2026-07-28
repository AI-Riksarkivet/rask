/**
 * The shared state every lineage workbench panel reads.
 *
 * Panels are mounted IMPERATIVELY by `@rask/dockview`, so they do not inherit the workbench page's
 * context tree — anything they need must be threaded through the dock's `context` prop. That is the
 * whole reason this key is explicit rather than an ambient import: one `LineageState` is polled once
 * by the workbench and shared by the graph, the run list and the event feed, so no two panels can be
 * a poll apart, and a panel dragged into a floating group keeps reading the same store it always did.
 */
import { getContext } from 'svelte';
import type { LineageState } from '$lib/lineage/store.svelte';

export const LINEAGE_STATE = Symbol('rask.lineage-state');

export function getLineageState(): LineageState {
	const store = getContext<LineageState | undefined>(LINEAGE_STATE);
	if (store === undefined) {
		throw new Error(
			'lineage panel mounted without a LineageState — pass it in the Dock `context` map under LINEAGE_STATE',
		);
	}
	return store;
}
