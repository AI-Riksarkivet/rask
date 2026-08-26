<script lang="ts" module>
	import MedallionNode, { type MedallionNodeType } from '$lib/lineage/MedallionNode.svelte';
	import JobNode, { type JobNodeType } from '$lib/lineage/JobNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline.
	const nodeTypes: NodeTypes = { medallion: MedallionNode, job: JobNode };
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
	import { Search } from '@lucide/svelte';
	import { FlowAutoFit } from '@rask/flow';
	import type { LineageState } from '$lib/lineage/store.svelte';
	import { useColorMode } from '@rask/ui/color-mode';
	import { LAYER } from '@rask/api/lineage';
	import { depths, elkLayout, layout } from '@rask/flow';

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
	 * Jump-to-node. With 81 nodes on the canvas there was no way to reach one you could name — you
	 * panned until you saw it, or you did not find it. Marquez has the same affordance in its header.
	 *
	 * Matches against the nodes CURRENTLY BUILT rather than the store, so it can never offer a node
	 * the canvas is not showing; picking one focuses it, which is also what re-roots the graph.
	 */
	let query = $state('');
	const matches = $derived.by(() => {
		const q = query.trim().toLowerCase();
		if (q.length < 2) return [];
		return nodes
			.map((n) => ({ id: n.id, name: String(n.data.id ?? '') }))
			.filter((n) => n.name.toLowerCase().includes(q))
			.slice(0, 8);
	});

	function jump(id: string): void {
		focusNode = id.startsWith(JOB_PREFIX)
			? { kind: 'job', name: id.slice(JOB_PREFIX.length) }
			: { kind: 'dataset', name: id.slice(DATASET_PREFIX.length) };
		query = '';
	}

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

	let nodes = $state.raw<FlowNode[]>([]);
	let edges = $state.raw<
		{
			id: string;
			source: string;
			target: string;
			animated: boolean;
			type: string;
			markerEnd: { type: MarkerType; width: number; height: number; color: string };
			style?: string;
		}[]
	>([]);

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
		// DEPTH IS GRAPH HOPS, exactly as Marquez counts it (`?depth=N` handed straight to its
		// lineage API, defaulting to 2 in `TableLevel.tsx`). Because the graph alternates kinds, one
		// hop off a dataset reaches its JOBS and two reaches the tables those jobs touch — so an odd
		// depth legitimately ends on a job, and 2 is the default for the same reason it is theirs.
		//
		// This counted LOGICAL hops (doubled) at first, which made depth 1 mean "runs plus their
		// tables". Nicer in isolation, and wrong: the number then meant something different here than
		// in the tool it is modelled on, to anyone comparing the two.
		//
		// Filtering happens BEFORE layout on purpose: laying out the full graph and then hiding
		// nodes would leave the survivors on their estate-wide coordinates, scattered across a
		// canvas whose other occupants are gone.
		if (focused && focusDepth !== null && ids.includes(focused)) {
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
			// Captured: the outer guard narrows `focusDepth`, but a closure does not inherit that
			// narrowing, and widening the guard instead would let `null` (= All) reach the loop.
			const hops = focusDepth;
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

		const depth = depths(ids, derive);
		const place = layout(ids, derive, (id) => depth.get(id) ?? 0);

		// Hand the SAME id/edge set to ELK for the phases `layout()` skips — coordinate assignment,
		// dummy-node edge routing and component packing. Untracked: this reads `nodes` to re-place
		// them, and tracking that would make the effect retrigger itself forever.
		const generation = ++buildGeneration;
		void elkLayout(ids, derive)
			.then((elk) => {
				if (generation !== buildGeneration || elk.size === 0) return;
				untrack(() => {
					nodes = nodes.map((n) => {
						const p = elk.get(n.id);
						return p ? { ...n, position: p } : n;
					});
				});
			})
			.catch(() => {
				// A failed layout is not a failed graph: the sync placement is already on screen and
				// correct enough to read, so this degrades rather than blanking the canvas.
			});

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
				type: 'smoothstep',
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
			return;
		}
		focusNode = raw.startsWith(JOB_PREFIX)
			? { kind: 'job', name: raw.slice(JOB_PREFIX.length) }
			: { kind: 'dataset', name: raw.slice(DATASET_PREFIX.length) };
	}
</script>

<section class="graph">
	<SvelteFlow
		bind:nodes
		bind:edges
		{nodeTypes}
		colorMode={theme.current}
		fitView
		{fitViewOptions}
		minZoom={MIN_ZOOM}
		onnodeclick={openNode}
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
		<Panel position="top-left">
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
								<button class="hit" onclick={() => jump(m.id)}>
									<span class="hkind">{m.id.startsWith(JOB_PREFIX) ? 'job' : 'data'}</span>
									<span class="hname">{m.name}</span>
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
					title="graph hops from the focused node — a hop alternates dataset and job, as in Marquez"
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
	</SvelteFlow>
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
