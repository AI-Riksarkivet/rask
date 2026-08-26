<script lang="ts" module>
	import MedallionNode, { type MedallionNodeType } from '$lib/lineage/MedallionNode.svelte';
	import JobNode, { type JobNodeType } from '$lib/lineage/JobNode.svelte';
	import { ElbowEdge } from '@rask/flow';
	import type { EdgeTypes, NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline.
	const nodeTypes: NodeTypes = { medallion: MedallionNode, job: JobNode };
	// The routed edge. ELK computes a route around the nodes in the way; `smoothstep` re-derives one
	// from the two endpoints and draws straight through them. `ElbowEdge` uses the first while it is
	// still accurate and falls back to the second once a node has been dragged.
	const edgeTypes: EdgeTypes = { elbow: ElbowEdge };
	type FlowNode = MedallionNodeType | JobNodeType;
</script>

<script lang="ts">
	/**
	 * The lineage DAG canvas — extracted from `routes/lineage/+page.svelte` so the same graph can be a
	 * full page AND a dock panel without either copying the layout maths.
	 *
	 * It takes its `LineageState` as a prop rather than constructing one: the page owns a store it
	 * polls itself, and a page owns ONE store shared across everything it renders, so the graph, the run
	 * list and the event feed can never be a poll apart. `buildMs` is exposed as a bindable readout for
	 * the page header that used to compute it inline.
	 */
	import { untrack } from 'svelte';
	import {
		SvelteFlow,
		Background,
		BackgroundVariant,
		Controls,
		MiniMap,
		Panel,
		MarkerType,
		type FitViewOptions,
	} from '@xyflow/svelte';
	import { ArrowUpRight, Crosshair, Minimize2, RefreshCw, Search, X } from '@lucide/svelte';
	import { FlowAutoFit, FlowCenterOn } from '@rask/flow';
	import type { LineageState } from '$lib/lineage/store.svelte';
	import { useColorMode } from '@rask/ui/color-mode';
	import { LAYER } from '@rask/api/lineage';
	import { depths, elkLayout, layout, resolveCollisions, routeKey } from '@rask/flow';
	import type { ElkRoute } from '@rask/flow';

	/**
	 * `base` and `navigate` arrive as PROPS rather than from `$app/paths` and `$app/navigation`.
	 *
	 * A package cannot import `$app/*` — the aliases only exist inside a SvelteKit app, and `@rask/ui`
	 * already establishes the rule (it imports none and detects the browser with
	 * `typeof window !== 'undefined'`). Handing the two zone-shaped values in is what lets this graph
	 * render under different bases (standalone dev vs behind the ingress).
	 */
	let {
		store,
		base = '',
		navigate,
		buildMs = $bindable(0),
		focusNode = $bindable(null),
		focusDepth = $bindable(2),
		collapsed = $bindable([]),
	}: {
		store: LineageState;
		base?: string;
		navigate?: (href: string) => void;
		buildMs?: number;
		/**
		 * The node the graph is rooted on, and how far around it to draw. BINDABLE and stated in
		 * PUBLIC terms — `{kind, name}`, never the prefixed node id — so a caller can put focus in
		 * the URL without depending on this component's id convention.
		 *
		 * That is what Marquez does: its ActionBar writes `searchParams.set('depth', …)` and its
		 * graph route carries `?tableLevelNode=`, so a focused graph is bookmarkable and shareable.
		 * This zone already uses the same idiom one route over (`/lineage/columns?dataset=`).
		 */
		focusNode?: { kind: 'dataset' | 'job'; name: string } | null;
		focusDepth?: number | null;
		/**
		 * Nodes whose downstream is folded away, as PREFIXED ids (`dataset:…` / `job:…`).
		 *
		 * BINDABLE and URL-shaped for the same reason focus is: Marquez carries its collapsed set in
		 * `?collapsedNodes=`, and a graph you have spent a minute pruning is worth linking to. The
		 * prefixed form is used here rather than the public `{kind,name}` because this is a SET —
		 * a list of objects in a query string is unreadable, and the prefix already encodes the kind.
		 */
		collapsed?: string[];
	} = $props();

	// The canvas follows the estate theme LIVE (the shell's theme button toggles `.dark` on
	// <html>). It used to be pinned to `colorMode="dark"`, which painted a black canvas inside
	// the light shell.
	const theme = useColorMode();

	/** Screen-space gutters kept clear of nodes so the floating overlays — the plane toggle
	 * (top-left Panel), the zoom Controls (bottom-left) and the MiniMap (bottom-right) — never
	 * land on top of a card. Shared by the initial `fitView` and every re-fit. */
	const FIT_PADDING: FitViewOptions['padding'] = {
		top: '76px',
		right: '28px',
		bottom: '136px',
		left: '28px',
	};
	const fitViewOptions = { padding: FIT_PADDING, maxZoom: 1 };
	/** Svelte Flow's default `minZoom` is 0.5 — on the real estate (60+ datasets) that clamp meant
	 * `fitView` could not frame the graph at all. Let the fit zoom out far enough to contain it. */
	const MIN_ZOOM = 0.1;

	// ONE graph, interleaving datasets and jobs — the shape Marquez draws (`routes/table-level`,
	// whose canvas holds TableLineageDatasetNode AND TableLineageJobNode). It used to offer two more
	// planes, Datasets (dataset→dataset) and Jobs (job→job), and both were PROJECTIONS that dropped
	// every edge crossing the kind boundary — which is why the estate read as scattered islands:
	// measured live, 51 dataset nodes with 18 edges and 25 of them isolated.
	//
	// They are gone rather than kept beside this one. Marquez has no such modes: its Datasets and Jobs
	// are browsable LISTS in the sidenav, which this zone already has at /lineage/datasets and
	// /lineage/jobs. Nothing is lost — job→job is readable here as job → dataset → job with the
	// mediating table visible, and the catalog's own `derived_from` edges are folded in below wherever
	// no run explains a pair.

	type JobAgg = {
		author?: string | null;
		state?: string | null;
		inputs: Set<string>;
		outputs: Set<string>;
		failed: boolean;
	};

	/**
	 * Fold the run-event feed into one entry per job: what it read, what it wrote, and how its most
	 * recent run ended.
	 *
	 * Oldest-first (`.reverse()` — the feed arrives newest-first) so the LAST write of each field
	 * wins and `state`/`author` end up describing the most recent run of that job.
	 *
	 * Note the union is per JOB NAME across every run, which is what makes a job that reads a table
	 * in one run and writes it in another look like a cycle — see the guard where the edges are built.
	 */
	function collectJobs(): Map<string, JobAgg> {
		const jobs = new Map<string, JobAgg>();
		for (const ev of [...store.events].reverse()) {
			if (!ev.job) continue;
			const j = jobs.get(ev.job) ?? {
				author: ev.author,
				state: ev.event_type,
				inputs: new Set<string>(),
				outputs: new Set<string>(),
				failed: false,
			};
			j.author = ev.author ?? j.author;
			j.state = ev.event_type ?? j.state;
			if (/FAIL|ABORT/i.test(ev.event_type ?? '')) j.failed = true;
			for (const o of ev.outputs ?? []) j.outputs.add(o);
			for (const i of ev.inputs ?? []) j.inputs.add(i);
			jobs.set(ev.job, j);
		}
		return jobs;
	}

	/**
	 * Node-id namespaces. The canvas holds BOTH kinds, and a job id and a dataset id come from
	 * different namespaces and could collide; the prefix also makes a click self-describing, so
	 * focus and the Open link read the KIND off the node itself.
	 *
	 * Spelled exactly as Marquez spells it — `generateNodeId` in `web/src/helpers/nodes.ts` returns
	 * `` `${type.toLowerCase()}:${namespace}:${name}` `` — so the two are cross-referenceable rather
	 * than merely similar. Marquez's own graph is the same shape: `LineageNode` carries
	 * `type: 'JOB' | 'DATASET'` in ONE node list with a flat `inEdges`/`outEdges` list, and
	 * `LineageJob` carries `inputs`/`outputs`, which is the bipartite relation drawn below.
	 */
	const JOB_PREFIX = 'job:';
	const DATASET_PREFIX = 'dataset:';
	const jobId = (job: string) => `${JOB_PREFIX}${job}`;
	const dsId = (ds: string) => `${DATASET_PREFIX}${ds}`;

	/** The prefixed id the graph filters on — derived from the public prop, never stored twice. */
	const focused = $derived(
		focusNode ? (focusNode.kind === 'job' ? jobId(focusNode.name) : dsId(focusNode.name)) : null,
	);
	const focusedName = $derived(focusNode?.name ?? null);
	const FOCUS_DEPTHS: (number | null)[] = [1, 2, 3, null];

	/**
	 * FOCUS DRIVES THE FETCH, not just the filter. This is P2 #12 / P1 #7.
	 *
	 * Unfocused, the store reads the estate — every visible dataset, hard-capped. That cap is a
	 * GLOBAL window, so a table five hops upstream of whatever you focus can sit outside it and stay
	 * unreachable at every depth: the buttons below re-filtered something already fetched, and no
	 * setting could pull in what the window had cut. Rooting the read on the focused dataset removes
	 * the ceiling from the answer entirely — the server walks the neighbourhood and sends it.
	 *
	 * THE DEPTH SENT IS DATASET HOPS; the buttons count ALTERNATING hops (dataset → job → dataset).
	 * Sending `focusDepth` unchanged therefore over-fetches slightly, and that direction is chosen
	 * deliberately: the server's neighbourhood must CONTAIN the one drawn, or the filter below would
	 * blank out neighbours the user asked for. Halving it would be tighter and occasionally wrong,
	 * and a lineage graph that silently omits an upstream is worse than one that fetched too much.
	 *
	 * DATASETS ONLY — the rooted endpoint is rooted on a dataset. A focused JOB falls back to the
	 * estate read and narrows client-side off the event feed, which is what it always did.
	 *
	 * `untrack` is load-bearing: `refocus` writes `store.focus` and then reads it inside `poll()`,
	 * so tracking that read would make this effect depend on its own write and re-run forever.
	 */
	$effect(() => {
		const name = focusNode?.kind === 'dataset' ? focusNode.name : null;
		const depth = focusDepth;
		untrack(() => void store.refocus(name !== null && depth !== null ? { name, depth } : null));
	});

	/**
	 * Jump-to-node. With 81 nodes on the canvas there was no way to reach one you could name — you
	 * panned until you saw it, or you did not find it. Marquez has the same affordance in its header.
	 *
	 * IT SEARCHES THE ESTATE, NOT THE CANVAS (P1 #8). Matching only drawn nodes was defended as
	 * deliberate — never offer what the canvas cannot show — and it is a closed loop: the canvas is
	 * capped when unfocused and bounded when focused, so the tables you most need to jump to are
	 * exactly the ones off it. Picking an off-canvas dataset focuses it, and focusing is now what
	 * FETCHES it, so the answer arrives instead of being withheld.
	 *
	 * On-canvas hits come first and are not re-fetched; the governed `/search` fills the rest and its
	 * rows are marked, because "somewhere in the estate" and "over there on screen" are different
	 * promises and a person about to lose their current view should be told which they are taking.
	 */
	let query = $state('');
	let offCanvas = $state.raw<{ id: string; name: string }[]>([]);

	const onCanvas = $derived.by(() => {
		const q = query.trim().toLowerCase();
		if (q.length < 2) return [];
		return nodes
			.map((n) => ({ id: n.id, name: String(n.data.id ?? '') }))
			.filter((n) => n.name.toLowerCase().includes(q))
			.slice(0, 8);
	});

	const matches = $derived.by(() => {
		const drawn = new Set(onCanvas.map((m) => m.id));
		const rest = offCanvas.filter((m) => !drawn.has(m.id)).map((m) => ({ ...m, off: true }));
		return [...onCanvas.map((m) => ({ ...m, off: false })), ...rest].slice(0, 8);
	});

	/**
	 * The estate half of the search, debounced.
	 *
	 * Debounced because this is a keystroke-driven request against a governed endpoint and a person
	 * typing a table name would otherwise fire one per character. The cancel flag is not the same
	 * thing as the timer: a request already in flight when the query changes must not be allowed to
	 * land, or a slower earlier response overwrites a faster later one and the list shows hits for a
	 * prefix the box no longer contains.
	 */
	$effect(() => {
		const q = query.trim();
		if (q.length < 2) {
			offCanvas = [];
			return;
		}
		let cancelled = false;
		const timer = setTimeout(() => {
			void store
				.searchEstate(q)
				.then((hits) => {
					if (!cancelled) offCanvas = hits.map((h) => ({ id: dsId(h.name), name: h.name }));
				})
				.catch(() => {
					// A failed search is an empty search, not a broken box: the on-canvas half still
					// answers, and the alternative is an error state over a type-ahead.
					if (!cancelled) offCanvas = [];
				});
		}, 180);
		return () => {
			cancelled = true;
			clearTimeout(timer);
		};
	});

	function jump(id: string): void {
		focusNode = id.startsWith(JOB_PREFIX)
			? { kind: 'job', name: id.slice(JOB_PREFIX.length) }
			: { kind: 'dataset', name: id.slice(DATASET_PREFIX.length) };
		query = '';
		offCanvas = [];
		drawerOpen = true;
	}

	/**
	 * THE DETAIL DRAWER (P1 #9). Marquez's node click navigates AND opens a drawer in one gesture,
	 * because "which node is this" and "what is in it" are the same question when you are reading a
	 * graph. rask's click only re-rooted, and every fact about the node lived a page-load away — so
	 * answering it meant leaving the graph, and coming back meant rebuilding the focus you left.
	 *
	 * It reads what the canvas ALREADY HAS rather than fetching: the store's node metadata, the
	 * folded event feed, the run board. That is a deliberate ceiling — this is the summary that makes
	 * the graph legible, and the detail page stays the place for the full record (readers, columns,
	 * governance edits), which is why "Open" is still one click away. A drawer that fetched would
	 * fire a request per click on a canvas whose whole idiom is clicking around.
	 */
	const jobAggs = $derived(collectJobs());

	type Detail =
		| {
				kind: 'dataset';
				name: string;
				namespace: string | null;
				sourceUri: string | null;
				tags: string[];
				versions: string[];
				failed: boolean;
				runState: string | null;
				producedBy: string[];
				readBy: string[];
		  }
		| {
				kind: 'job';
				name: string;
				author: string | null;
				state: string | null;
				failed: boolean;
				inputs: string[];
				outputs: string[];
		  };

	/**
	 * Everything the canvas knows about one node. Extracted from the drawer's derived value because
	 * the HOVER CARD asks the identical question about a different node — and a hover card computed
	 * from a second, slightly-different expression is how the two end up disagreeing.
	 */
	function describe(f: { kind: 'dataset' | 'job'; name: string } | null): Detail | null {
		if (!f) return null;
		if (f.kind === 'dataset') {
			const meta = store.nodes.find((n) => n.id === f.name);
			return {
				kind: 'dataset',
				name: f.name,
				namespace: meta?.namespace ?? null,
				sourceUri: meta?.source_uri ?? null,
				tags: meta?.tags ?? [],
				versions: meta?.versions ?? [],
				failed: meta?.failed ?? false,
				runState: runStateByDataset[f.name] ?? null,
				producedBy: [...jobAggs].filter(([, j]) => j.outputs.has(f.name)).map(([n]) => n),
				readBy: [...jobAggs].filter(([, j]) => j.inputs.has(f.name)).map(([n]) => n),
			};
		}
		const j = jobAggs.get(f.name);
		return {
			kind: 'job',
			name: f.name,
			author: j?.author ?? null,
			state: j?.state ?? null,
			failed: j?.failed ?? false,
			inputs: [...(j?.inputs ?? [])],
			outputs: [...(j?.outputs ?? [])],
		};
	}

	const detail = $derived(describe(focusNode));

	/**
	 * THE HOVER CARD (P3). Every node on this canvas carried a native `title`, which is a browser
	 * tooltip: it appears after a fixed delay the page cannot influence, renders in the OS's styling,
	 * truncates, and can only ever be one string — so a dataset's location, versions, tags and run
	 * state had to be flattened into a sentence or left out. Marquez shows a real card.
	 *
	 * It is the drawer's own summary, smaller, without the navigation. A click still opens the drawer;
	 * this is for the pass over the canvas where you are identifying nodes, not reading one.
	 */
	let hoverId = $state<string | null>(null);
	let hoverAt = $state<{ x: number; y: number } | null>(null);
	/** The hovered node, as the public `{kind, name}` shape — `null` for the focused node, whose
	 *  facts are already open in the drawer beside it. */
	const hoverTarget = $derived(
		!hoverId || hoverId === focused
			? null
			: hoverId.startsWith(JOB_PREFIX)
				? ({ kind: 'job', name: hoverId.slice(JOB_PREFIX.length) } as const)
				: ({ kind: 'dataset', name: hoverId.slice(DATASET_PREFIX.length) } as const),
	);
	const hoverDetail = $derived(describe(hoverTarget));

	/**
	 * Opening is DELAYED, closing is immediate.
	 *
	 * Without the delay a card fires under the pointer for every node crossed while panning or
	 * reaching for another one, which is the failure mode that makes hover cards feel hostile. The
	 * asymmetry is the point: appearing is a decision, disappearing is not.
	 */
	let hoverTimer: ReturnType<typeof setTimeout> | null = null;
	const HOVER_DELAY = 380;

	function onNodeEnter(e: unknown) {
		const ev = e as { node?: { id: string }; event?: { clientX?: number; clientY?: number } };
		const id = ev.node?.id;
		if (!id) return;
		const x = ev.event?.clientX;
		const y = ev.event?.clientY;
		if (hoverTimer) clearTimeout(hoverTimer);
		hoverTimer = setTimeout(() => {
			hoverAt = x !== undefined && y !== undefined ? { x, y } : null;
			hoverId = id;
		}, HOVER_DELAY);
	}

	function onNodeLeave() {
		if (hoverTimer) clearTimeout(hoverTimer);
		hoverTimer = null;
		hoverId = null;
	}

	/** A pending card must not open after the component is gone. */
	$effect(() => () => {
		if (hoverTimer) clearTimeout(hoverTimer);
	});

	/**
	 * COMPACT CARDS. Marquez ships the same switch, and the reason is measurable here: a full card
	 * renders 51–129px tall against the 64 ELK is told, which is what makes cards collide and what
	 * forces `fitView` to zoom out past legibility on a real estate. Compact drops description — the
	 * URI, the version chips, the tags, a job's outputs — and keeps everything a reader would act on,
	 * including the failure state. It also feeds ELK a smaller box, so the layout tightens rather
	 * than just the cards.
	 *
	 * Not persisted: it is a way of looking at the graph you are looking at now, not a preference.
	 */
	let compact = $state(false);

	/**
	 * MANUAL REFRESH. The poll is cursor-gated — it re-reads when the lineage cursor moves — which is
	 * right almost always and unhelpful in the one case someone reaches for a refresh button: when
	 * they have just done something elsewhere and want to see whether it landed. The button is also
	 * the honest place to say the canvas is not frozen.
	 */
	let refreshing = $state(false);
	async function refresh(): Promise<void> {
		if (refreshing) return;
		refreshing = true;
		try {
			await store.poll();
		} finally {
			refreshing = false;
		}
	}

	/** Nonce for the centring gesture — bumped per press, because pressing again with the same node
	 *  selected must move the viewport again. */
	let centerNonce = $state(0);

	/** How many nodes the current collapse set folded away — reported, because a graph that quietly
	 *  got smaller is a graph you cannot trust. Assigned by the build; read by the header strip. */
	let foldedAway = $state(0);

	/**
	 * Fold or unfold one node's downstream. Immutable reassign — `collapsed` is a bindable prop that
	 * the page mirrors into the URL, and mutating the array in place would not notify it.
	 */
	function toggleCollapse(id: string): void {
		collapsed = collapsed.includes(id) ? collapsed.filter((c) => c !== id) : [...collapsed, id];
	}

	/** Open state is EXPLICIT, set by the gestures that select a node, rather than derived from
	 *  `focusNode`. Dismissing the drawer must not also drop the focus — the neighbourhood on screen
	 *  is what you were reading — and deriving it would make the close button un-closeable. */
	let drawerOpen = $state(false);

	/** Where the focus bar's "Open" goes — the detail page for whatever is focused. */
	const focusedHref = $derived(
		focusNode
			? `${base}/lineage/${focusNode.kind === 'job' ? 'jobs' : 'datasets'}/${encodeURIComponent(focusNode.name)}`
			: null,
	);

	/**
	 * ELK is ASYNC, and the build below is not. The sync `layout()` still runs first so the canvas
	 * paints immediately with usable coordinates; ELK's better ones land a tick later and are applied
	 * only if no newer build has started since — otherwise a slow layout for a stale graph would
	 * overwrite the positions of the graph actually on screen. That race is real here because the
	 * store polls, and focusing a node rebuilds while a previous run may still be resolving.
	 */
	let buildGeneration = 0;

	/**
	 * The graph SHAPE the last ELK run was asked about — ids plus edges, order-independent.
	 *
	 * ELK is re-run only when this changes, which is the same guard Marquez uses
	 * (`useLayout.ts` compares the new ELK input against a ref and returns the old one when equal,
	 * so a data-only change never re-lays out).
	 *
	 * WITHOUT IT THIS DESTROYS USER STATE. The store reassigns fresh arrays on every poll, so the
	 * build effect re-runs whenever the estate ticks; an unconditional ELK call then overwrote every
	 * node's position on each successful poll, and a node the user had DRAGGED snapped back within
	 * seconds. `store.svelte.ts`'s own comment says its failure guard exists so a blip "never …
	 * destroys dragged node positions" — this path was destroying them on success instead. Positions
	 * otherwise carry forward through `prev`, so skipping the run is exactly what preserves a drag.
	 *
	 * A real shape change still re-lays out everything, dragged nodes included. That is Marquez's
	 * behaviour too, and it is the honest trade: the alternative is a graph that never re-flows.
	 */
	let lastElkShape = '';

	let nodes = $state.raw<FlowNode[]>([]);
	let edges = $state.raw<
		{
			id: string;
			source: string;
			target: string;
			animated: boolean;
			type: string;
			data?: { route?: ElkRoute };
			markerEnd: { type: MarkerType; width: number; height: number; color: string };
			style?: string;
		}[]
	>([]);

	/**
	 * The routes ELK computed for the CURRENT layout, keyed by the derivation pair.
	 *
	 * Held separately from `edges` because the two change on different clocks: the edge list is
	 * rebuilt on every data tick, while a layout runs only when the graph's shape changes. Folding
	 * routes into the edge build would drop them on the first tick after a layout and put every edge
	 * back on `smoothstep` until the shape changed again.
	 */
	let elkRoutes = $state.raw<Map<string, ElkRoute>>(new Map());

	// Re-fit the viewport only when the node-set or the view changes (not on every data poll).
	// `focused`/`focusDepth` ride the key as well as the node ids: focusing a node whose
	// neighbourhood happens to be the whole graph leaves the id list identical, and the viewport
	// would then keep whatever pan the previous selection left it on.
	const fitKey = $derived(focused + '|' + focusDepth + '|' + nodes.map((n) => n.id).join(','));

	// Current run-state per dataset: the latest run (by updated_at) that lists it as an output.
	const runStateByDataset = $derived.by(() => {
		const m: Record<string, string> = {};
		const ordered = [...store.runs].sort((a, b) =>
			(a.updated_at ?? '').localeCompare(b.updated_at ?? ''),
		);
		for (const r of ordered) {
			if (!r.state) continue;
			for (const out of r.outputs ?? []) m[out] = r.state;
		}
		return m;
	});

	// Rebuild the active graph plane whenever the polled data (or the chosen view) changes.
	// Reconcile, don't rebuild: keep each node's identity + dragged position across the poll.
	$effect(() => {
		// Read the current nodes UNTRACKED — only their last positions carry forward; tracking
		// `nodes` (the var we reassign below) would make this effect retrigger itself.
		const prev = new Map(untrack(() => nodes).map((node) => [node.id, node]));
		// UNTRACKED, and for the same reason `prev` is: this effect ASSIGNS `elkRoutes` when a layout
		// resolves, so a tracked read here would make the effect depend on its own output and re-run
		// the entire build on every layout. The `.then` below patches the live edges directly, so the
		// only thing this read owes is the routes as of now.
		const knownRoutes = untrack(() => elkRoutes);
		const t0 = performance.now();

		/**
		 * Which side of the focused node each node sits on — the question a lineage graph exists to
		 * answer, and the one Marquez models explicitly (`findUpstreamNodes` / `findDownstreamNodes`
		 * in its own `table-level/layout.ts`). "Where did this come from" and "what breaks if I
		 * change it" are different questions, and an undifferentiated blob answers neither.
		 *
		 * A PLAIN LOCAL, deliberately. It was `$state.raw` first, which this effect both assigned and
		 * read back three lines later — so the effect depended on its own output and re-ran forever.
		 * The page did not error, it PEGGED: every later measurement read a frozen DOM, and even the
		 * browser automation stopped answering. Nothing outside this effect needs the value.
		 */
		const relation: Record<string, 'focus' | 'upstream' | 'downstream'> = {};

		// THE graph — the interleaved DAG: input dataset → job → output dataset.
		//
		// The layout maths is reused rather than reinvented. `depths`/`layout` read
		// DERIVATION-oriented edges (`source` derived from `target`, target one column left), so
		// emitting "the job derives from each input" and "each output derives from the job" makes
		// the two kinds alternate columns on their own — dataset d, job d+1, dataset d+2 — with no
		// separate interleaving pass. Rendering then reverses them, exactly as the datasets plane
		// does, so arrows read upstream → downstream.
		const jobs = collectJobs();
		const dsMeta = new Map(store.nodes.map((n) => [n.id, n]));

		// Every dataset the EVENTS name, not just the governed bulk read. An edge whose endpoint
		// is not in the node set is dropped by Svelte Flow silently, which would delete exactly
		// the connections this plane exists to draw.
		const dsSet = new Set<string>(dsMeta.keys());
		for (const j of jobs.values()) {
			for (const i of j.inputs) dsSet.add(i);
			for (const o of j.outputs) dsSet.add(o);
		}

		// `let`, not `const`: the focus pass below narrows it to the neighbourhood of one node.
		let derive: { source: string; target: string }[] = [];
		// Pairs a job already explains, as `<output>|<input>` — the derivation this run mediates.
		const mediated = new Set<string>();
		for (const [job, j] of jobs) {
			// A dataset a job both READS and WRITES is dropped from the read side, and this is
			// load-bearing rather than tidy: `collectJobs` unions across every run of a job name,
			// so one job that reads a table in one run and writes it in another becomes
			// ds → job → ds — a two-cycle. `depths` is longest-path with an iteration cap, so a
			// cycle does not hang, it INFLATES: measured live at 160 layers and a 38,430px canvas,
			// which `fitView` could only answer by clamping to minZoom and rendering 20px nodes.
			//
			// It is not hypothetical and not only a data artefact. It showed up here because the
			// promotion-outcome emit mis-attributed gold promotions to `embed_features` (fixed in
			// 68d61716, though the historical events keep the shape), and any genuinely
			// incremental job — read the table, write the table — has it by nature.
			for (const i of j.inputs) {
				if (j.outputs.has(i)) continue;
				derive.push({ source: jobId(job), target: dsId(i) });
			}
			for (const o of j.outputs) derive.push({ source: dsId(o), target: jobId(job) });
			for (const o of j.outputs) for (const i of j.inputs) if (i !== o) mediated.add(`${o}|${i}`);
		}

		// FALL BACK to the catalog's own `derived_from` edge wherever no run explains the pair.
		// The event feed is a bounded window and the graph is not, so a table whose producing run
		// has aged out has no job to hang from — and drawing only event-derived edges made those
		// tables MORE isolated than the datasets plane, not less (measured live: isolated datasets
		// 25 → 33 before this fallback, 25 after). Suppressed where a job does explain the pair, or
		// the same derivation would be drawn twice: once through the run and once around it.
		for (const e of store.edges) {
			if (mediated.has(`${e.source}|${e.target}`)) continue;
			if (!dsSet.has(e.source) || !dsSet.has(e.target)) continue;
			derive.push({ source: dsId(e.source), target: dsId(e.target) });
		}

		let ids = [...[...dsSet].map(dsId), ...[...jobs.keys()].map(jobId)];

		// FOCUS — Marquez's `?nodeId=&depth=N`, which is the part that makes a lineage graph
		// legible at estate scale: it never draws the whole estate, only the neighbourhood of one
		// node. Without it this canvas is 79 nodes at 1,200×2,750px, a 1:2.3 aspect in a 16:9
		// viewport, so `fitView` clamps to minZoom and every card renders ~20px wide.
		//
		// DEPTH IS ALTERNATING-NODE HOPS ON THE CLIENT, and it is NOT the same unit as Marquez's.
		// Because this graph alternates kinds, one hop off a dataset reaches its JOBS and two reaches
		// the tables those jobs touch, so an odd depth legitimately ends on a job.
		//
		// DO NOT re-assert equivalence with Marquez here — two earlier revisions did, in this comment
		// and in the control's own tooltip, and both were wrong. Marquez's `?depth=N` is JOB hops
		// evaluated SERVER-side: `LineageDao.java`'s recursive CTE seeds `0 AS depth` from the rooted
		// JOB and recurses while `depth < :depth`, stepping job→job on "shares any dataset", and then
		// attaches every one of those jobs' datasets REGARDLESS of depth. Its root is a job even when
		// you ask about a dataset. Their depth=2 lands nearer this graph's hop 6 than its hop 2.
		//
		// The default of 2 is shared, and that is a coincidence of taste, not of unit.
		//
		// Matching the unit is not simply a counting change: theirs bounds what the SERVER FETCHES,
		// while this bounds a filter over an already-fetched, capped window. See P1 item 7 in
		// `open_lineage_graph.md` — the two belong in one change or neither.
		//
		// Filtering happens BEFORE layout on purpose: laying out the full graph and then hiding
		// nodes would leave the survivors on their estate-wide coordinates, scattered across a
		// canvas whose other occupants are gone.
		//
		// COLOURING AND NARROWING ARE SEPARATE (P1 #6). This guard used to require a finite depth for
		// both, so switching to "All" — the moment the graph gets big enough for direction to be the
		// only thing making it readable — dropped the upstream/downstream shading entirely and left an
		// undifferentiated estate with one node marked. Direction is a property of the focus, not of
		// the window: it is computed whenever something is focused, and only the `keep` filter below
		// is skipped when depth is All.
		if (focused && ids.includes(focused)) {
			const adj = new Map<string, string[]>();
			for (const e of derive) {
				(adj.get(e.source) ?? adj.set(e.source, []).get(e.source)!).push(e.target);
				(adj.get(e.target) ?? adj.set(e.target, []).get(e.target)!).push(e.source);
			}
			// DIRECTED walks first, so a node can be told apart from its neighbours by WHICH WAY it
			// lies. `derive` is derivation-oriented (source derived FROM target), so following
			// source→target walks upstream and target→source walks downstream.
			const up = new Map<string, string[]>();
			const down = new Map<string, string[]>();
			for (const e of derive) {
				(up.get(e.source) ?? up.set(e.source, []).get(e.source)!).push(e.target);
				(down.get(e.target) ?? down.set(e.target, []).get(e.target)!).push(e.source);
			}
			// `null` (= All) means "walk until the frontier is empty", which terminates on its own:
			// `seen` is checked before a node is enqueued, so every node is expanded at most once.
			// Captured in a local because a closure does not inherit the narrowing above.
			const hops = focusDepth ?? Number.POSITIVE_INFINITY;
			const reach = (side: Map<string, string[]>): Set<string> => {
				const seen = new Set<string>();
				let wave = [focused];
				for (let hop = 0; hop < hops && wave.length > 0; hop += 1) {
					const next: string[] = [];
					for (const id of wave) {
						for (const n of side.get(id) ?? []) {
							if (n === focused || seen.has(n)) continue;
							seen.add(n);
							next.push(n);
						}
					}
					wave = next;
				}
				return seen;
			};
			const upstreamSet = reach(up);
			const downstreamSet = reach(down);
			relation[focused] = 'focus';
			for (const id of upstreamSet) relation[id] = 'upstream';
			// Downstream wins a tie: a node reachable BOTH ways is on a cycle or a diamond, and
			// "what depends on this" is the answer a person is usually acting on.
			for (const id of downstreamSet) relation[id] = 'downstream';

			// Only NARROWING is depth-gated — a BLOCK, not an early return: everything after this
			// `if (focused …)` is the layout, and bailing out of the effect here would leave the
			// canvas holding the previous build.
			if (focusDepth !== null) {
				const keep = new Set<string>([focused]);
				let frontier = [focused];
				for (let hop = 0; hop < focusDepth && frontier.length > 0; hop += 1) {
					const next: string[] = [];
					for (const id of frontier) {
						for (const n of adj.get(id) ?? []) {
							if (keep.has(n)) continue;
							keep.add(n);
							next.push(n);
						}
					}
					frontier = next;
				}
				ids = ids.filter((id) => keep.has(id));
				derive = derive.filter((e) => keep.has(e.source) && keep.has(e.target));
				// Deleting the CURRENT entry during `for…of` is defined behaviour for Set and Map, so
				// these iterate the live collections rather than a copy.
				for (const id of dsSet) if (!keep.has(dsId(id))) dsSet.delete(id);
				for (const job of jobs.keys()) if (!keep.has(jobId(job))) jobs.delete(job);
			}
		}

		/**
		 * COLLAPSE: fold away what a node feeds, and nothing else.
		 *
		 * The rule is "hidden iff EVERY thing that feeds it is collapsed or hidden", reached as a
		 * fixpoint from the downstream closure of the collapsed set. The naive version — hide the
		 * whole downstream reachable set — is wrong on any real lineage graph: a gold table fed by
		 * two silver tables would vanish when you collapsed one of them, which silently deletes a
		 * dataset that is still very much being produced. Depth already answers "show me less";
		 * collapse has to answer "show me less OF THIS ONE" or it is just a second depth control.
		 */
		const collapsedSet = new Set(collapsed);
		if (collapsedSet.size > 0) {
			// `derive` is derivation-oriented: source is derived FROM target, so target FEEDS source.
			const fedBy = new Map<string, string[]>();
			const feeds = new Map<string, string[]>();
			for (const e of derive) {
				(fedBy.get(e.source) ?? fedBy.set(e.source, []).get(e.source)!).push(e.target);
				(feeds.get(e.target) ?? feeds.set(e.target, []).get(e.target)!).push(e.source);
			}
			const hidden = new Set<string>();
			const queue = [...collapsedSet];
			while (queue.length > 0) {
				const id = queue.shift();
				if (id === undefined) break;
				for (const next of feeds.get(id) ?? []) {
					// A collapsed node is never hidden by another collapse — you must still be able to
					// see, and un-collapse, the thing you collapsed.
					if (hidden.has(next) || collapsedSet.has(next)) continue;
					hidden.add(next);
					queue.push(next);
				}
			}
			// Relax to a fixpoint. Deletions are COLLECTED and applied per round rather than made
			// during iteration: `every()` below reads `hidden`, so removing entries mid-pass would
			// make a node's verdict depend on the order its siblings happened to be visited in.
			for (;;) {
				const freed: string[] = [];
				for (const id of hidden) {
					const sources = fedBy.get(id) ?? [];
					if (!sources.every((f) => collapsedSet.has(f) || hidden.has(f))) freed.push(id);
				}
				if (freed.length === 0) break;
				for (const id of freed) hidden.delete(id);
			}
			if (hidden.size > 0) {
				ids = ids.filter((id) => !hidden.has(id));
				derive = derive.filter((e) => !hidden.has(e.source) && !hidden.has(e.target));
				for (const id of dsSet) if (hidden.has(dsId(id))) dsSet.delete(id);
				for (const job of jobs.keys()) if (hidden.has(jobId(job))) jobs.delete(job);
			}
			foldedAway = hidden.size;
		} else {
			foldedAway = 0;
		}

		const depth = depths(ids, derive);
		const place = layout(ids, derive, (id) => depth.get(id) ?? 0);

		// Hand the SAME id/edge set to ELK for the phases `layout()` skips — coordinate assignment,
		// dummy-node edge routing and component packing. Untracked: this reads `nodes` to re-place
		// them, and tracking that would make the effect retrigger itself forever.
		// SORTED on BOTH halves. Sorting only the edges left this order-SENSITIVE, and the feed does
		// not promise a stable order: a poll that returned the identical graph in a different
		// order read as a shape change, re-ran ELK and snapped a dragged node back. Measured —
		// the drag reverted across a poll whose node and edge counts were byte-identical.
		// `compact` rides the key: the switch changes every node's BOX, and a layout computed for the
		// old boxes is the wrong layout for the new ones. Without it the cards shrink and the spacing
		// stays where it was, which looks like the switch half-worked.
		const shape = `${compact}|${[...ids].sort().join(',')}|${derive
			.map((e) => `${e.source}>${e.target}`)
			.sort()
			.join(',')}`;
		if (shape !== lastElkShape) {
			lastElkShape = shape;
			const generation = ++buildGeneration;
			// REAL BOXES, not a constant. `elkLayout`'s `size` hook existed and nothing ever passed it,
			// so every node was declared 200×64 to a layout engine whose whole job is reserving space.
			// Measured on the deployed estate: cards render 51–129px tall, 82 of 85 exceed the declared
			// 64, and 22 node pairs ACTUALLY OVERLAP — a dataset card sitting 31px into a job card.
			//
			// Svelte Flow writes `measured` after it renders a node, so the PREVIOUS render is the
			// source: first pass falls back to the per-kind card width and ELK's own default height,
			// and every pass after that reserves what is really drawn. Marquez feeds its real box the
			// same way (`34 + fields.length * 10` for a dataset).
			const measuredSize = (id: string) => {
				const p = prev.get(id);
				const w = p?.measured?.width;
				const h = p?.measured?.height;
				// JobNode is 210px, MedallionNode 200px — a 10px difference that only matters as a
				// fallback, since a measured node reports its own width anyway.
				// The COMPACT fallbacks are the compact cards' real widths — measured falls back to
				// declared only on the first pass, but that first pass is the one that decides whether
				// the switch visibly tightens the layout.
				const fallbackW = compact ? 152 : id.startsWith(JOB_PREFIX) ? 210 : 200;
				return {
					width: w && w > 0 ? w : fallbackW,
					height: h && h > 0 ? h : compact ? 34 : 64,
				};
			};
			void elkLayout(ids, derive, { size: measuredSize })
				.then((elk) => {
					if (generation !== buildGeneration || elk.nodes.size === 0) return;
					untrack(() => {
						nodes = nodes.map((n) => {
							const p = elk.nodes.get(n.id);
							return p ? { ...n, position: p } : n;
						});
						// The routing phase's output, finally read. Attached to the live edges here as
						// well as stored, so the edges already on screen pick up the route without
						// waiting for the next data tick to rebuild them.
						elkRoutes = elk.routes;
						edges = edges.map((e) => {
							const route = elk.routes.get(routeKey(e.target, e.source));
							return route ? { ...e, data: { route } } : e;
						});
					});
					// ONE FRAME LATER the cards have rendered and Svelte Flow has measured them, which is
					// the only moment the real geometry exists. ELK reserved an ESTIMATE (a card's height
					// depends on how many chip rows wrap), so this separates whatever still collides —
					// measured on the deployed estate: 22 overlapping pairs, one by 200×31px.
					//
					// Deliberately inside the ELK branch, so it runs only when the graph is re-laid out.
					// A drag never triggers it, which is what keeps it from shoving a node the user
					// placed on purpose.
					requestAnimationFrame(() => {
						if (generation !== buildGeneration) return;
						untrack(() => {
							nodes = resolveCollisions(nodes, { overlapThreshold: 0.5, margin: 8 });
						});
					});
				})
				.catch(() => {
					// A failed layout is not a failed graph: the sync placement is already on screen
					// and correct enough to read, so this degrades rather than blanking the canvas.
					// Reset the shape so the next tick retries rather than caching the failure.
					lastElkShape = '';
				});
		}

		const dsNodes: FlowNode[] = [...dsSet].map((id) => {
			const meta = dsMeta.get(id);
			// Depth counts BOTH kinds here, so a dataset sits on every second column — halve it
			// to recover the medallion ramp tier the datasets plane assigns.
			const tier = Math.floor((depth.get(dsId(id)) ?? 0) / 2);
			return {
				id: dsId(id),
				type: 'medallion' as const,
				position: prev.get(dsId(id))?.position ?? place.get(dsId(id)) ?? { x: 0, y: 0 },
				data: {
					id,
					layer: LAYER[id] ?? Math.min(tier, 4),
					source_uri: meta?.source_uri,
					tags: meta?.tags ?? [],
					versions: meta?.versions ?? [],
					failed: meta?.failed ?? false,
					// The focused card, marked. `.selected` is already styled by MedallionNode and
					// JobNode and was simply never set by anything — without it the node a click
					// focused looked exactly like the twelve it dragged in with it.
					selected: dsId(id) === focused,
					rel: relation[dsId(id)] ?? null,
					runState: runStateByDataset[id] ?? null,
					compact,
					collapsed: collapsedSet.has(dsId(id)),
					onCollapse: toggleCollapse,
				},
			};
		});

		const jobNodes: FlowNode[] = [...jobs.entries()].map(([job, j]) => ({
			id: jobId(job),
			type: 'job' as const,
			position: prev.get(jobId(job))?.position ?? place.get(jobId(job)) ?? { x: 0, y: 0 },
			data: {
				id: job.replace(/^ray-jobs\//, ''),
				author: j.author,
				state: j.state,
				outputs: [...j.outputs],
				failed: j.failed,
				selected: jobId(job) === focused,
				rel: relation[jobId(job)] ?? null,
				compact,
				collapsed: collapsedSet.has(jobId(job)),
				onCollapse: toggleCollapse,
			},
		}));

		nodes = [...dsNodes, ...jobNodes];
		// An edge takes the class of the node it POINTS AT, so a chain reads as one colour the whole
		// way out from the focus rather than changing hue at every hop.
		const EDGE_TINT = {
			upstream: 'var(--primary)',
			downstream: 'var(--amber)',
			focus: 'var(--ink)',
			none: 'var(--line)',
		} as const;
		edges = derive.map((e) => {
			const tint = EDGE_TINT[relation[e.source] ?? relation[e.target] ?? 'none'];
			return {
				id: `${e.target}->${e.source}`,
				source: e.target,
				target: e.source,
				// Only the focused neighbourhood animates. Eighty crawling dashes is not information,
				// it is a screensaver — and it made the one chain a reader cared about impossible to
				// pick out of the rest.
				animated: focused !== null,
				type: 'elbow',
				data: { route: knownRoutes.get(routeKey(e.source, e.target)) },
				// A DAG without arrowheads is an undirected blob: "A relates to B somehow" instead of
				// "A produced B". This is the single cheapest legibility win on the canvas.
				markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: tint },
				style: `stroke:${tint}`,
			};
		});
		buildMs = Math.round((performance.now() - t0) * 10) / 10;
	});

	// A node click opens the detail page (Marquez parity): dataset node → dataset detail,
	// job node → job detail.
	function openNode(e: unknown) {
		const ev = e as { node?: { id: string }; targetNode?: { id: string } };
		const raw = ev.node?.id ?? ev.targetNode?.id ?? null;
		if (!raw) return;
		// A click FOCUSES rather than navigates. Marquez does BOTH in one gesture, because its graph
		// route is parameterized by a node (`/lineage/:type/:namespace/:name`), so navigating IS
		// re-rooting. This zone's graph is estate-level and owns no such route, so the two are split:
		// the click re-roots here, and navigation moves to the focus bar's "Open", where it is
		// labelled rather than implied. Clicking the focused node again clears it.
		if (focused === raw) {
			focusNode = null;
			drawerOpen = false;
			return;
		}
		focusNode = raw.startsWith(JOB_PREFIX)
			? { kind: 'job', name: raw.slice(JOB_PREFIX.length) }
			: { kind: 'dataset', name: raw.slice(DATASET_PREFIX.length) };
		// One gesture, both answers — the Marquez behaviour this was missing.
		drawerOpen = true;
	}

	/** The drawer's link to this dataset's column-level graph — the zone's existing route, which
	 *  already takes the dataset as a query parameter. */
	const columnsHref = $derived(
		detail?.kind === 'dataset'
			? `${base}/lineage/columns?dataset=${encodeURIComponent(detail.name)}`
			: null,
	);

	/** Both drawer links go through the injected navigator when there is one, and fall through to a
	 *  real href otherwise — same reason the focus bar's Open does. */
	function go(e: MouseEvent, href: string): void {
		if (!navigate) return;
		e.preventDefault();
		navigate(href);
	}
</script>

<section class="graph">
	<SvelteFlow
		bind:nodes
		bind:edges
		{nodeTypes}
		{edgeTypes}
		colorMode={theme.current}
		fitView
		{fitViewOptions}
		minZoom={MIN_ZOOM}
		onnodeclick={openNode}
		onnodepointerenter={onNodeEnter}
		onnodepointerleave={onNodeLeave}
		onmovestart={onNodeLeave}
	>
		<Background variant={BackgroundVariant.Dots} gap={16} />
		<Controls position="bottom-left" />
		<!-- Themed surface + bottom-right corner: it used to float a hardcoded near-black
		     rectangle over the cards in the light shell. -->
		<MiniMap
			pannable
			zoomable
			position="bottom-right"
			width={150}
			height={104}
			bgColor="var(--panel)"
			nodeColor="var(--primary)"
			nodeStrokeColor="var(--line)"
			maskColor="color-mix(in srgb, var(--panel-2) 72%, transparent)"
		/>
		<FlowAutoFit trigger={fitKey} padding={FIT_PADDING} />
		<FlowCenterOn nodeId={focused} nonce={centerNonce} />
		<Panel position="top-left">
			<div class="viewbar">
				<!-- DENSITY + FRESHNESS, the two view-level controls Marquez keeps beside its graph. -->
				<button
					class="vbtn"
					class:on={compact}
					aria-pressed={compact}
					title="compact cards — name and status only, and a tighter layout"
					onclick={() => (compact = !compact)}
				>
					<Minimize2 size={12} />
					<span>Compact</span>
				</button>
				{#if collapsed.length > 0}
					<button
						class="vbtn on"
						title="{foldedAway} node{foldedAway === 1
							? ''
							: 's'} folded behind {collapsed.length} collapsed node{collapsed.length ===
						1
							? ''
							: 's'} — click to expand them all"
						onclick={() => (collapsed = [])}
					>
						<span>{foldedAway} folded</span>
					</button>
				{/if}
				<button
					class="vbtn"
					disabled={refreshing}
					title="re-read the lineage feed now"
					aria-label="Refresh the lineage feed"
					onclick={() => void refresh()}
				>
					<RefreshCw size={12} class={refreshing ? 'spin' : undefined} />
				</button>
			</div>
			<div class="searchbar">
				<Search size={12} />
				<input
					class="sinput"
					type="search"
					placeholder="find a dataset or job…"
					aria-label="Find a node"
					bind:value={query}
					onkeydown={(e) => {
						if (e.key === 'Enter' && matches[0]) jump(matches[0].id);
						if (e.key === 'Escape') query = '';
					}}
				/>
				{#if matches.length > 0}
					<ul class="hits">
						{#each matches as m (m.id)}
							<li>
								<button
									class="hit"
									title={m.off
										? 'not on the canvas — picking it fetches its neighbourhood'
										: m.name}
									onclick={() => jump(m.id)}
								>
									<span class="hkind">{m.id.startsWith(JOB_PREFIX) ? 'job' : 'data'}</span>
									<span class="hname">{m.name}</span>
									{#if m.off}<span class="hoff">fetch</span>{/if}
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
			<div class="focusbar">
				{#if focused}
					<span class="fname" title={focusedName}>{focusedName}</span>
					<span class="fsep">·</span>
				{:else}
					<span class="fhint">click a node to focus</span>
					<span class="fsep">·</span>
				{/if}
				<span
					class="flabel"
					title="hops from the focused node — one hop reaches its jobs, two the tables those jobs touch. On a focused dataset the neighbourhood is read from the server at this depth, so a larger number fetches more, it does not merely reveal more."
					>hops</span
				>
				{#each FOCUS_DEPTHS as d (d ?? 'all')}
					<button
						class="fd"
						class:on={focusDepth === d}
						disabled={!focused}
						aria-pressed={focusDepth === d}
						onclick={() => (focusDepth = d)}
					>
						{d ?? 'All'}
					</button>
				{/each}
				{#if focused}
					<!-- CENTRE. Search, the drawer's relation links and a click all re-root the graph, and
					     each of them can leave the selected node somewhere off-screen. Disabled rather
					     than hidden when nothing is focused, per the estate's show-disabled rule. -->
					<button
						class="fd ficon"
						title="centre the view on the focused node"
						aria-label="Centre on the focused node"
						onclick={() => (centerNonce += 1)}
					>
						<Crosshair size={12} />
					</button>
				{/if}
				{#if focused && focusedHref}
					<!-- A real anchor (middle-click, copy-link, keyboard) that hands the actual navigation
					     to the injected `navigate`: this component cannot import `$app/navigation`,
					     which is the whole reason that prop exists. Falls through to the href when no
					     navigator was passed. -->
					<a
						class="fopen"
						href={focusedHref}
						onclick={(e) => {
							if (!navigate) return;
							e.preventDefault();
							navigate(focusedHref);
						}}>Open</a
					>
					<button class="fclear" onclick={() => (focusNode = null)}>Clear</button>
				{/if}
			</div>
		</Panel>
		{#if detail && drawerOpen}
			<!-- A canvas PANEL, not a modal sheet. A lineage drawer is read WHILE looking at the
			     graph — which node lights up as upstream, where the chain goes — so anything that
			     dims or blocks the canvas defeats the reason for opening it. `nowheel` keeps a
			     scroll inside the drawer from zooming the viewport underneath. -->
			<Panel position="top-right">
				<aside class="drawer nowheel" aria-label="Node detail">
					<header class="dhead">
						<span class="dkind" class:job={detail.kind === 'job'}>
							{detail.kind === 'job' ? 'job' : 'dataset'}
						</span>
						<h2 class="dtitle" title={detail.name}>{detail.name}</h2>
						<button class="dclose" aria-label="Close detail" onclick={() => (drawerOpen = false)}>
							<X size={13} />
						</button>
					</header>

					{#if detail.kind === 'dataset'}
						<dl class="dgrid">
							{#if detail.namespace}
								<dt>namespace</dt>
								<dd>{detail.namespace}</dd>
							{/if}
							{#if detail.sourceUri}
								<dt>location</dt>
								<dd class="dmono" title={detail.sourceUri}>{detail.sourceUri}</dd>
							{/if}
							{#if detail.runState}
								<dt>last run</dt>
								<dd class:dbad={detail.failed}>{detail.runState}</dd>
							{/if}
							{#if detail.versions.length > 0}
								<dt>versions</dt>
								<dd class="dchips">
									{#each detail.versions.slice(-6) as v (v)}<span class="dchip">v{v}</span>{/each}
								</dd>
							{/if}
							{#if detail.tags.length > 0}
								<dt>tags</dt>
								<dd class="dchips">
									{#each detail.tags as t (t)}<span class="dchip">{t}</span>{/each}
								</dd>
							{/if}
						</dl>
						<!-- Producers and consumers are the two questions a lineage drawer exists for, and
						     they are ALWAYS shown, empty or not: "nothing reads this table" is an answer
						     someone acts on, and hiding the row makes it indistinguishable from a drawer
						     that simply does not report it. -->
						<section class="drel">
							<h3>produced by</h3>
							{#if detail.producedBy.length > 0}
								<ul>
									{#each detail.producedBy as j (j)}
										<li>
											<button class="dlink" onclick={() => jump(jobId(j))}
												>{j.replace(/^ray-jobs\//, '')}</button
											>
										</li>
									{/each}
								</ul>
							{:else}
								<p class="dnone">no run in the event window wrote this table</p>
							{/if}
							<h3>read by</h3>
							{#if detail.readBy.length > 0}
								<ul>
									{#each detail.readBy as j (j)}
										<li>
											<button class="dlink" onclick={() => jump(jobId(j))}
												>{j.replace(/^ray-jobs\//, '')}</button
											>
										</li>
									{/each}
								</ul>
							{:else}
								<p class="dnone">nothing in the event window reads this table</p>
							{/if}
						</section>
					{:else}
						<dl class="dgrid">
							{#if detail.state}
								<dt>state</dt>
								<dd class:dbad={detail.failed}>{detail.state}</dd>
							{/if}
							{#if detail.author}
								<dt>author</dt>
								<dd>{detail.author}</dd>
							{/if}
						</dl>
						<section class="drel">
							<h3>reads</h3>
							{#if detail.inputs.length > 0}
								<ul>
									{#each detail.inputs as d (d)}
										<li><button class="dlink" onclick={() => jump(dsId(d))}>{d}</button></li>
									{/each}
								</ul>
							{:else}
								<p class="dnone">no inputs recorded</p>
							{/if}
							<h3>writes</h3>
							{#if detail.outputs.length > 0}
								<ul>
									{#each detail.outputs as d (d)}
										<li><button class="dlink" onclick={() => jump(dsId(d))}>{d}</button></li>
									{/each}
								</ul>
							{:else}
								<p class="dnone">no outputs recorded</p>
							{/if}
						</section>
					{/if}

					<footer class="dfoot">
						{#if focusedHref}
							<a class="dgo" href={focusedHref} onclick={(e) => go(e, focusedHref)}>
								Full detail <ArrowUpRight size={12} />
							</a>
						{/if}
						{#if columnsHref}
							<a class="dgo" href={columnsHref} onclick={(e) => go(e, columnsHref)}>
								Columns <ArrowUpRight size={12} />
							</a>
						{/if}
					</footer>
				</aside>
			</Panel>
		{/if}
	</SvelteFlow>
	{#if hoverDetail && hoverAt}
		<!-- FIXED positioning against the pointer's viewport coordinates: the canvas pans and zooms
		     under its own transform, and a card placed in canvas space would scale with it and drift
		     while the graph moves. `pointer-events: none` so the card can never intercept the click
		     that would open the real drawer. -->
		<aside
			class="hovercard"
			style:left="{hoverAt.x + 16}px"
			style:top="{hoverAt.y + 12}px"
			aria-hidden="true"
		>
			<div class="hchead">
				<span class="hckind" class:job={hoverDetail.kind === 'job'}>
					{hoverDetail.kind === 'job' ? 'job' : 'dataset'}
				</span>
				<span class="hcname">{hoverDetail.name}</span>
			</div>
			{#if hoverDetail.kind === 'dataset'}
				{#if hoverDetail.sourceUri}
					<div class="hcuri mono">{hoverDetail.sourceUri}</div>
				{/if}
				<div class="hcrow">
					{#if hoverDetail.runState}
						<span class="hcstate" class:bad={hoverDetail.failed}>{hoverDetail.runState}</span>
					{/if}
					{#if hoverDetail.versions.length}
						<span class="hcdim"
							>{hoverDetail.versions.length} version{hoverDetail.versions.length === 1
								? ''
								: 's'}</span
						>
					{/if}
					<span class="hcdim">
						{hoverDetail.producedBy.length} producer{hoverDetail.producedBy.length === 1 ? '' : 's'}
						· {hoverDetail.readBy.length} consumer{hoverDetail.readBy.length === 1 ? '' : 's'}
					</span>
				</div>
				{#if hoverDetail.tags.length}
					<div class="hcrow">
						{#each hoverDetail.tags.slice(0, 4) as t (t)}<span class="hctag">{t}</span>{/each}
					</div>
				{/if}
			{:else}
				<div class="hcrow">
					{#if hoverDetail.state}
						<span class="hcstate" class:bad={hoverDetail.failed}>{hoverDetail.state}</span>
					{/if}
					{#if hoverDetail.author}<span class="hcdim">{hoverDetail.author}</span>{/if}
				</div>
				<div class="hcrow">
					<span class="hcdim">
						reads {hoverDetail.inputs.length} · writes {hoverDetail.outputs.length}
					</span>
				</div>
			{/if}
			<div class="hchint">click to open detail</div>
		</aside>
	{/if}

	{#if store.settled && store.online && nodes.length === 0}
		<div class="empty">
			<b>No lineage yet.</b><br />
			Datasets and jobs appear here as pipelines emit OpenLineage events; browse the
			<a href="{base}/lineage/datasets">Datasets</a> view for the governed catalog.
		</div>
	{:else if store.settled && !store.online && nodes.length === 0}
		<div class="empty">
			<b>Lineage service unreachable.</b><br />
			Retrying every few seconds — the graph renders as soon as the feed answers.
		</div>
	{/if}
</section>

<style>
	.graph {
		position: relative;
		flex: 1 1 0;
		min-height: 0;
		min-width: 0;
	}
	/* Sits clear of the top-left plane toggle (which is a flow Panel, not part of this box). */
	.empty {
		position: absolute;
		top: 76px;
		left: 28px;
		color: var(--mut);
		font-size: 13px;
		line-height: 1.7;
	}
	.empty a {
		color: var(--ink);
	}
	/* The focus bar sits UNDER the plane toggle in the same top-left Panel, so `FIT_PADDING.top`
	   (76px) already keeps nodes clear of both — it was sized for the toggle plus this row. */
	.focusbar {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-top: 6px;
		padding: 3px 8px;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: 999px;
		box-shadow: var(--shadow);
		font-size: 11px;
		color: var(--mut);
	}
	.fname {
		max-width: 190px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--ink);
		font-weight: 600;
	}
	.fhint {
		font-style: italic;
	}
	.fsep {
		opacity: 0.5;
	}
	.flabel {
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-size: 10px;
	}
	.fd {
		min-width: 20px;
		padding: 2px 6px;
		border: none;
		background: transparent;
		color: var(--mut);
		font-size: 11px;
		font-weight: 600;
		border-radius: 999px;
		cursor: pointer;
	}
	.fd:hover:not(:disabled) {
		color: var(--ink);
	}
	.fd.on:not(:disabled) {
		color: var(--primary);
		background: color-mix(in srgb, var(--primary) 14%, transparent);
	}
	/* Depth is meaningless with nothing focused, so the buttons are DISABLED rather than hidden —
	   the control stays where the eye already found it, and its state says why it is inert. */
	.fd:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.fopen,
	.fclear {
		padding: 2px 8px;
		border: 1px solid var(--line);
		background: transparent;
		color: var(--ink);
		font-size: 11px;
		font-weight: 600;
		border-radius: 999px;
		cursor: pointer;
		text-decoration: none;
	}
	.fopen:hover,
	.fclear:hover {
		border-color: var(--primary);
		color: var(--primary);
	}
	/* Give the minimap the panel surface (border + radius + shadow) so it reads as a chrome
	   overlay in both themes instead of a floating dark rectangle. */
	:global(.svelte-flow__minimap) {
		border: 1px solid var(--line);
		border-radius: var(--radius);
		box-shadow: 0 6px 20px -10px rgb(0 0 0 / 45%);
		overflow: hidden;
	}

	.searchbar {
		position: relative;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: 999px;
		box-shadow: var(--shadow);
		color: var(--mut);
	}
	.sinput {
		width: 190px;
		border: none;
		background: transparent;
		color: var(--ink);
		font-size: 11px;
		outline: none;
	}
	.sinput::placeholder {
		color: var(--faint);
	}
	/* The hit list hangs BELOW the bar and out of flow, so opening it never reflows the focus bar
	   underneath — which would move the depth buttons out from under the pointer mid-click. */
	.hits {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		margin: 0;
		padding: 4px;
		list-style: none;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: 10px;
		box-shadow: var(--shadow);
		max-height: 220px;
		overflow-y: auto;
		z-index: 5;
	}
	.hit {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		padding: 4px 6px;
		border: none;
		background: transparent;
		border-radius: 6px;
		cursor: pointer;
		text-align: left;
	}
	.hit:hover {
		background: color-mix(in srgb, var(--primary) 12%, transparent);
	}
	/* The view-level controls, above the search box. Same floating-panel language as the bars below
	   it — this is one stack of controls, not three unrelated widgets. */
	.viewbar {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-bottom: 6px;
		padding: 3px;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: color-mix(in srgb, var(--panel) 92%, transparent);
		backdrop-filter: blur(6px);
		width: fit-content;
	}
	.vbtn {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 3px 7px;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--muted);
		font: inherit;
		font-size: 11px;
		cursor: pointer;
	}
	.vbtn:hover:not(:disabled) {
		background: color-mix(in srgb, var(--ink) 8%, transparent);
		color: var(--ink);
	}
	.vbtn.on {
		color: var(--primary);
		background: color-mix(in srgb, var(--primary) 14%, transparent);
	}
	.vbtn:disabled {
		opacity: 0.55;
		cursor: default;
	}
	/* One turn per refresh, so the button says "I am doing it" without a second spinner element. */
	:global(.viewbar .spin) {
		animation: vspin 0.9s linear infinite;
	}
	@keyframes vspin {
		to {
			transform: rotate(360deg);
		}
	}
	.ficon {
		display: inline-flex;
		align-items: center;
		padding: 2px 5px;
	}

	/* THE HOVER CARD. Fixed to the viewport, never interactive — the click underneath belongs to the
	   node, and a card that swallows it would make hovering a node stop you opening it. */
	.hovercard {
		position: fixed;
		z-index: 60;
		pointer-events: none;
		max-width: 280px;
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 7px 9px;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: color-mix(in srgb, var(--panel) 96%, transparent);
		backdrop-filter: blur(6px);
		box-shadow: 0 6px 18px color-mix(in srgb, var(--ink) 16%, transparent);
		font-size: 11px;
	}
	.hchead {
		display: flex;
		align-items: center;
		gap: 5px;
		min-width: 0;
	}
	.hckind {
		flex: none;
		font-size: 9px;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 0 4px;
		border-radius: 4px;
		color: var(--primary);
		background: color-mix(in srgb, var(--primary) 14%, transparent);
	}
	.hckind.job {
		color: var(--amber);
		background: color-mix(in srgb, var(--amber) 16%, transparent);
	}
	.hcname {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.hcuri {
		font-size: 10px;
		color: var(--muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.hcrow {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.hcstate {
		font-size: 10px;
		font-weight: 600;
		color: var(--ok);
	}
	.hcstate.bad {
		color: var(--fail);
	}
	.hcdim {
		font-size: 10px;
		color: var(--muted);
	}
	.hctag {
		font-size: 9px;
		padding: 0 4px;
		border-radius: 4px;
		color: var(--muted);
		background: color-mix(in srgb, var(--ink) 7%, transparent);
	}
	.hchint {
		font-size: 9px;
		color: var(--faint);
	}

	/* THE DETAIL DRAWER. Height-capped and scrollable rather than growing: a job with forty inputs
	   would otherwise run the panel off the bottom of the canvas, taking its footer links with it. */
	.drawer {
		width: 268px;
		max-height: min(62vh, 520px);
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 10px 12px 12px;
		border: 1px solid var(--line);
		border-radius: 10px;
		background: color-mix(in srgb, var(--panel) 94%, transparent);
		backdrop-filter: blur(6px);
		box-shadow: 0 8px 24px color-mix(in srgb, var(--ink) 12%, transparent);
		font-size: 11px;
	}
	.dhead {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.dkind {
		flex: none;
		font-size: 9px;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 1px 5px;
		border-radius: 4px;
		color: var(--primary);
		background: color-mix(in srgb, var(--primary) 14%, transparent);
	}
	.dkind.job {
		color: var(--amber);
		background: color-mix(in srgb, var(--amber) 16%, transparent);
	}
	.dtitle {
		flex: 1;
		min-width: 0;
		margin: 0;
		font-size: 12px;
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dclose {
		flex: none;
		display: grid;
		place-items: center;
		width: 20px;
		height: 20px;
		padding: 0;
		border: none;
		border-radius: 5px;
		background: transparent;
		color: var(--muted);
		cursor: pointer;
	}
	.dclose:hover {
		background: color-mix(in srgb, var(--ink) 8%, transparent);
		color: var(--ink);
	}
	.dgrid {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 3px 10px;
		margin: 0;
	}
	.dgrid dt {
		color: var(--muted);
		font-size: 10px;
	}
	.dgrid dd {
		margin: 0;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.dmono {
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 10px;
		white-space: nowrap;
	}
	.dbad {
		color: var(--danger, #d9534f);
		font-weight: 600;
	}
	.dchips {
		display: flex;
		flex-wrap: wrap;
		gap: 3px;
	}
	.dchip {
		padding: 0 4px;
		border-radius: 4px;
		font-size: 9px;
		color: var(--muted);
		background: color-mix(in srgb, var(--ink) 7%, transparent);
	}
	.drel h3 {
		margin: 8px 0 3px;
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted);
	}
	.drel ul {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	/* Each relation is a BUTTON that re-roots the graph, not a link that leaves it: following a
	   producer is the move a reader makes next, and doing it in place is the whole advantage of
	   having the drawer over the canvas. */
	.dlink {
		display: block;
		width: 100%;
		padding: 2px 4px;
		border: none;
		border-radius: 4px;
		background: transparent;
		color: var(--primary);
		font: inherit;
		text-align: left;
		cursor: pointer;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dlink:hover {
		background: color-mix(in srgb, var(--primary) 12%, transparent);
	}
	.dnone {
		margin: 0;
		padding: 2px 4px;
		color: var(--muted);
		font-style: italic;
	}
	.dfoot {
		display: flex;
		gap: 10px;
		padding-top: 8px;
		border-top: 1px solid var(--line);
	}
	.dgo {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--primary);
		text-decoration: none;
		font-weight: 600;
	}
	.dgo:hover {
		text-decoration: underline;
	}

	/* An off-canvas hit costs a fetch and replaces the current view, so it is marked rather than
	   hidden — the estate half of the search is the point, not a fallback to apologise for. */
	.hoff {
		margin-left: auto;
		flex: none;
		font-size: 9px;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 1px 4px;
		border-radius: 4px;
		color: var(--primary);
		background: color-mix(in srgb, var(--primary) 14%, transparent);
	}
	.hkind {
		flex: none;
		font-size: 9px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--mut);
	}
	.hname {
		font-size: 11px;
		color: var(--ink);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
