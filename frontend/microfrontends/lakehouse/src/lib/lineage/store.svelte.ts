import { fetchEstateGraph, fetchEvents, fetchRuns } from '$lib/api';
import type { EventRecord, GraphEdge, GraphNode, RunStatus } from '@rask/api/lineage';

/** Hard cap on the nodes the DAG explorer renders — a DENSITY limit now, not a request budget:
 * the estate arrives in one bulk `/graph` read (per-node version/failed rollups included), so the
 * old per-dataset /producers + /graph fan-out (hundreds of requests per tick) is gone. The full
 * estate belongs to the paginated Datasets table; the header shows `capped` honestly when the
 * estate exceeds this window. */
const MAX_GRAPH = 60;

/** How many recent events feed the jobs plane (job identity/state/edges are folded from these). */
const EVENTS_WINDOW = 200;

/** Live lineage state for the DAG explorer, polled from the lineage service. Svelte 5 runes in a
 * class. List pages (Datasets / Jobs / Runs) fetch their own endpoints — this store only carries
 * what the graph view renders. One tick = three requests: /graph + /events + /runs. */
export class LineageState {
	nodes = $state<GraphNode[]>([]);
	edges = $state<GraphEdge[]>([]);
	events = $state<EventRecord[]>([]);
	runs = $state<RunStatus[]>([]);
	/** Total VISIBLE datasets the estate graph reports (may exceed the MAX_GRAPH window). */
	total = $state(0);
	/** True when the estate is larger than the graph window — the header says so honestly. */
	capped = $state(false);
	/** True once the FIRST poll settled (success or failure) — before that the UI says "connecting",
	 * never "waiting/offline" (the old header claimed WAITING while the canvas showed datasets). */
	settled = $state(false);
	online = $state(false);
	lastUpdated = $state('');

	/** Overlap guard: a slow tick must not stack behind the poll interval (§2 perf, 2026-07-11). */
	#polling = false;

	async poll(): Promise<void> {
		if (this.#polling) return;
		this.#polling = true;
		try {
			const [graph, events, runs] = await Promise.all([
				fetchEstateGraph(MAX_GRAPH),
				fetchEvents({ limit: EVENTS_WINDOW, summary: true }),
				fetchRuns(),
			]);

			// HARD-FAILURE GUARD (audit B1): `getJSON` maps timeout / 4xx / 5xx / network error to
			// null — indistinguishable from "empty". Assign only what actually RESOLVED: a failed
			// slice PRESERVES its last good state (never blanks the canvas or destroys dragged node
			// positions on a blip); `online` tracks the graph read, the view's backbone.
			if (runs) this.runs = runs.runs ?? [];
			if (events) this.events = events.events ?? [];
			if (graph === null) {
				this.online = false;
				return;
			}
			this.nodes = graph.nodes ?? [];
			this.edges = graph.edges ?? [];
			this.total = graph.total ?? this.nodes.length;
			this.capped = graph.capped ?? false;
			this.online = true;
			this.lastUpdated = new Date().toLocaleTimeString();
		} finally {
			this.settled = true;
			this.#polling = false;
		}
	}
}
