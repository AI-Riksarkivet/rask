import type { createLineageClient } from '@rask/api/lineage';
import type {
	EstateGraph,
	EventRecord,
	GraphEdge,
	GraphNode,
	LineageGraph,
	RunStatus,
} from '@rask/api/lineage';

/**
 * The lineage client this store polls through — INJECTED, not imported.
 *
 * It used to `import { fetchEstateGraph, … } from '$lib/api'`, which bound the store to one zone:
 * `$lib` resolves per app, and the lakehouse binding underneath is `createLineageClient(bff)` over
 * `createBffClient($app/paths)` — a package can import neither alias. Taking the client as a
 * constructor argument is the same stance `@rask/ui` takes with its client props, and it is what lets
 * ONE store back the lineage panels wherever they are mounted.
 */
export type LineageClient = ReturnType<typeof createLineageClient>;

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
 * what the graph view renders. One tick = three requests: a graph read + /events + /runs.
 *
 * THE GRAPH READ HAS TWO SCOPES, and which one runs is the whole of P2 #12. Unfocused it is the
 * estate read — every visible dataset, hard-capped at `MAX_GRAPH`. Focused on a dataset it is the
 * ROOTED read, `/datasets/{name}/graph?depth=N`, bounded SERVER-side. The distinction is not a
 * refinement of the same answer: the estate window is a global cap, so a table five hops upstream of
 * the focus can sit outside it and be unreachable at ANY depth, which is exactly what made the depth
 * control a filter over an already-fetched window rather than a control over what is fetched, and
 * what stopped search from reaching anything not already drawn. Rooted, both work, because focusing a name is what FETCHES its neighbourhood. */
export class LineageState {
	nodes = $state<GraphNode[]>([]);
	edges = $state<GraphEdge[]>([]);
	events = $state<EventRecord[]>([]);
	runs = $state<RunStatus[]>([]);
	/** Total VISIBLE datasets the estate graph reports (may exceed the MAX_GRAPH window). */
	total = $state(0);
	/** True when the estate is larger than the graph window — the header says so honestly. */
	capped = $state(false);
	/**
	 * The dataset neighbourhood to read instead of the estate, or `null` for the estate read.
	 *
	 * DATASETS ONLY, and `null` for a focused JOB, because the rooted endpoint is rooted on a
	 * dataset. A focused job still narrows the canvas client-side off the event feed; it just does
	 * not change what the server sends. Saying that here rather than silently sending a job name to
	 * a dataset route is the difference between a bounded read and a 404 that reads as "offline".
	 */
	focus = $state<{ name: string; depth: number } | null>(null);
	/** Which read produced the current `nodes`/`edges` — the header must not report a rooted
	 * neighbourhood using the estate's "N of M datasets, capped" sentence. */
	scope = $state<'estate' | 'rooted'>('estate');
	/** True once the FIRST poll settled (success or failure) — before that the UI says "connecting",
	 * never "waiting/offline" (the old header claimed WAITING while the canvas showed datasets). */
	settled = $state(false);
	online = $state(false);
	lastUpdated = $state('');

	/** Overlap guard: a slow tick must not stack behind the poll interval (§2 perf, 2026-07-11). */
	#polling = false;
	/** …but a poll REQUESTED during a slow tick must not be silently dropped either. The interval's
	 * ticks are interchangeable, so dropping one is free; a focus change is not — it is a user
	 * gesture asking for a different read, and swallowing it leaves the canvas showing the previous
	 * neighbourhood with the new depth button lit. Set when a call is refused, drained on unwind. */
	#requeued = false;

	#client: LineageClient;

	constructor(client: LineageClient) {
		this.#client = client;
	}

	/**
	 * Name-search the ESTATE, not the canvas — the governed `/search` the Datasets page already uses.
	 *
	 * The graph's own search matched only nodes currently drawn, which is a closed loop: you can find
	 * what you can already see. That is not a nitpick at estate scale — the estate read is capped and
	 * a focused read is bounded, so most of the estate is off-canvas by construction, and the one
	 * gesture that could get you there was the one that refused to look. Governance is the service's: `/search` filters to the caller's visible set BEFORE the
	 * limit, so this cannot surface — or count — a table the caller may not see.
	 */
	async searchEstate(q: string, limit = 8): Promise<{ name: string; matches: string[] }[]> {
		const found = await this.#client.fetchSearch(q, limit);
		return (found?.results ?? []).map((hit) => ({ name: hit.name, matches: hit.matches ?? [] }));
	}

	/** Point the graph read at one dataset's neighbourhood (or back at the estate) and read again
	 * NOW. Focus is a gesture, not a tick: waiting up to a full poll interval to honour it is what
	 * makes a depth button feel broken. */
	async refocus(focus: { name: string; depth: number } | null): Promise<void> {
		this.focus = focus;
		await this.poll();
	}

	async poll(): Promise<void> {
		if (this.#polling) {
			this.#requeued = true;
			return;
		}
		this.#polling = true;
		try {
			// Read ONCE, outside the await: `this.focus` can change while the requests are in flight,
			// and the scope reported below must describe the read that actually happened.
			const focus = this.focus;
			// Annotated at the binding rather than cast at the call: the two reads return DIFFERENT
			// shapes (a rooted graph has a `root`, an estate graph a `total`/`capped`), and widening
			// them here is what keeps the union honest at the point of use below.
			const graphRead: Promise<LineageGraph | EstateGraph | null> = focus
				? this.#client.fetchGraph(focus.name, focus.depth)
				: this.#client.fetchEstateGraph(MAX_GRAPH);
			const [graph, events, runs] = await Promise.all([
				graphRead,
				this.#client.fetchEvents({ limit: EVENTS_WINDOW, summary: true }),
				this.#client.fetchRuns(),
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
			// A rooted read carries neither field, and defaulting is not a formality: reporting a
			// stale estate `total` beside a 6-node neighbourhood, or `capped` from the last estate
			// tick, would have the header describe a window that is not on screen.
			this.total = 'total' in graph ? (graph.total ?? this.nodes.length) : this.nodes.length;
			this.capped = 'capped' in graph ? (graph.capped ?? false) : false;
			this.scope = focus ? 'rooted' : 'estate';
			this.online = true;
			this.lastUpdated = new Date().toLocaleTimeString();
		} finally {
			this.settled = true;
			this.#polling = false;
			if (this.#requeued) {
				this.#requeued = false;
				// Not awaited: `poll()`'s caller is an interval tick or a focus gesture, neither of
				// which should block on a read it did not ask for. Failures are already swallowed
				// per-slice above, so there is nothing here for a rejection handler to add.
				void this.poll();
			}
		}
	}
}
