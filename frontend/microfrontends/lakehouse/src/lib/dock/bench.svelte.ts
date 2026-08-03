/**
 * The lakehouse dock's shared lineage state — ONE `LineageState`, polled once, read by every panel.
 *
 * This is the per-zone dock's whole advantage: the graph, the run board and the event feed are the
 * zone's own components sharing the zone's own store through ordinary context, so they can never be
 * a poll apart, and the store reaches the lineage service through the zone's own client.
 */
import { createContext } from 'svelte';
import { createLineageClient } from '@rask/api/lineage';
import { bff } from '$lib/http';
import { LineageState } from '$lib/lineage/store.svelte';

export function createBench(): LineageState {
	// The zone binds the client; the store takes it injected — the same wiring the /lineage page uses.
	return new LineageState(createLineageClient(bff));
}

export const [getBench, setBench] = createContext<LineageState>();
