import { fetchColumnDownstream, fetchColumnGraph, fetchColumnUpstream } from '$lib/api';
import type { ColumnGraph, ColumnNeighbors } from '@rask/api/lineage';

/** Field-to-field lineage state for the Columns view (#24): one dataset's column subgraph plus the
 * focused field's provenance/impact. Svelte 5 runes in a class, with the latest-wins guards the
 * explorer grew through the 2026-07-13 bug hunt (a slow earlier fetch must never overwrite a newer
 * selection's result). */
export class ColumnLineageState {
	graph = $state<ColumnGraph | null>(null);
	/** True once the FIRST graph load settled, success or failure. Before that the view says
	 * "loading", never "no field lineage" — the same two-flag shape `LineageState` already ships,
	 * reused rather than re-invented as a tri-state. */
	settled = $state(false);
	/** True when the last graph read actually SUCCEEDED. `fetchColumnGraph` maps timeout / 4xx / 5xx /
	 * network error to `null`, which is indistinguishable from "this dataset has no column lineage" —
	 * so without this flag a dead lineage service is reported to the user as an empty result (#147). */
	online = $state(false);
	/** The field the user clicked — drives the provenance/impact panel. */
	selectedColumn = $state<{ dataset: string; field: string } | null>(null);
	upstream = $state<ColumnNeighbors | null>(null);
	downstream = $state<ColumnNeighbors | null>(null);

	/** Monotonic request ids — latest-wins for the subgraph and the per-field neighbor fetches. */
	#graphReq = 0;
	#fieldReq = 0;

	/**
	 * How many DATASET hops out from the root to read.
	 *
	 * Lives on the store rather than being passed per call because the poll re-reads on the lineage
	 * cursor and must not silently drop back to one hop on the next tick — which is exactly what a
	 * per-call parameter would do, and it would look like the control resetting itself.
	 */
	depth = $state(1);

	/** Load the column-level lineage subgraph for one dataset. */
	async loadGraph(name: string): Promise<void> {
		const req = ++this.#graphReq;
		const graph = await fetchColumnGraph(name, this.depth);
		// Latest-wins: an older in-flight request must not publish over a newer selection's result.
		if (req !== this.#graphReq) return;
		// A failed read PRESERVES the last good graph rather than blanking the canvas — the sibling
		// store's hard-failure guard, applied here for the same reason.
		if (graph !== null) {
			this.graph = graph;
			this.online = true;
		} else {
			this.online = false;
		}
		this.settled = true;
	}

	/** Load one FIELD's provenance (upstream) + impact (downstream). Clears FIRST (audit B2): stale
	 * neighbors must not render under a new field's header, and `null` means "unknown/loading" —
	 * distinct from an empty result. */
	async loadNeighbors(name: string, field: string): Promise<void> {
		const req = ++this.#fieldReq;
		this.upstream = null;
		this.downstream = null;
		const [upstream, downstream] = await Promise.all([
			fetchColumnUpstream(name, field),
			fetchColumnDownstream(name, field),
		]);
		if (req === this.#fieldReq) {
			this.upstream = upstream;
			this.downstream = downstream;
		}
	}
}
