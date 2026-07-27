import { fetchColumnDownstream, fetchColumnGraph, fetchColumnUpstream } from '$lib/api';
import type { ColumnGraph, ColumnNeighbors } from '@rask/api/lineage';

/** Field-to-field lineage state for the Columns view (#24): one dataset's column subgraph plus the
 * focused field's provenance/impact. Svelte 5 runes in a class, with the latest-wins guards the
 * explorer grew through the 2026-07-13 bug hunt (a slow earlier fetch must never overwrite a newer
 * selection's result). */
export class ColumnLineageState {
	graph = $state<ColumnGraph | null>(null);
	/** The field the user clicked — drives the provenance/impact panel. */
	selectedColumn = $state<{ dataset: string; field: string } | null>(null);
	upstream = $state<ColumnNeighbors | null>(null);
	downstream = $state<ColumnNeighbors | null>(null);

	/** Monotonic request ids — latest-wins for the subgraph and the per-field neighbor fetches. */
	#graphReq = 0;
	#fieldReq = 0;

	/** Load the column-level lineage subgraph for one dataset. */
	async loadGraph(name: string): Promise<void> {
		const req = ++this.#graphReq;
		const graph = await fetchColumnGraph(name);
		if (req === this.#graphReq) this.graph = graph;
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
