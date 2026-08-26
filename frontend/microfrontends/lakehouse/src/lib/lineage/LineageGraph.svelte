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
		type FitViewOptions,
	} from '@xyflow/svelte';
	import { FlowAutoFit } from '@rask/flow';
	import type { LineageState } from '$lib/lineage/store.svelte';
	import { useColorMode } from '@rask/ui/color-mode';
	import { LAYER } from '@rask/api/lineage';
	import { depths, layout } from '@rask/flow';

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
	}: {
		store: LineageState;
		base?: string;
		navigate?: (href: string) => void;
		buildMs?: number;
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
	 * Fold the run-event feed into one entry per job, plus which jobs produced each dataset.
	 *
	 * Shared by the Jobs and Pipeline planes deliberately: they are two renderings of one fact
	 * (which job read what and wrote what), and two copies of this fold would let them disagree
	 * about it while both looking plausible.
	 *
	 * Oldest-first (`.reverse()` — the feed arrives newest-first) so the LAST write of each field
	 * wins and `state`/`author` end up describing the most recent run of that job.
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
	 * Node-id namespaces for the pipeline plane, the one canvas holding BOTH kinds. A job id and a
	 * dataset id come from different namespaces and could collide; the prefix also makes a click
	 * self-describing, so routing reads the NODE rather than the active view.
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

	/** The focused node id (prefixed), and how many LOGICAL hops around it to draw — `null` is All.
	 *  Pipeline-plane only: it is the one plane where the estate view is unreadable, and the two
	 *  single-kind projections are small enough to click straight through. */
	let focused = $state<string | null>(null);
	let focusDepth = $state<number | null>(2);
	const FOCUS_DEPTHS: (number | null)[] = [1, 2, 3, null];

	/** The focused node's own name, unprefixed — the label the focus bar prints. */
	const focusedName = $derived(
		focused?.startsWith(JOB_PREFIX)
			? focused.slice(JOB_PREFIX.length)
			: focused?.startsWith(DATASET_PREFIX)
				? focused.slice(DATASET_PREFIX.length)
				: null,
	);
	/** Where the focus bar's "Open" goes — the same detail route a click used to navigate to. */
	const focusedHref = $derived.by(() => {
		if (!focused || !focusedName) return null;
		const kind = focused.startsWith(JOB_PREFIX) ? 'jobs' : 'datasets';
		return `${base}/lineage/${kind}/${encodeURIComponent(focusedName)}`;
	});

	let nodes = $state.raw<FlowNode[]>([]);
	let edges = $state.raw<
		{ id: string; source: string; target: string; animated: boolean; type: string }[]
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
		// A hop here is a LOGICAL one — dataset → job → dataset, i.e. TWO graph edges — because
		// this plane alternates the kinds. Depth 1 therefore reads as "the runs that touch this
		// table and the tables they touch", which is what a person means by one hop; counting raw
		// edges would make every odd depth end on a job and look truncated.
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
			const keep = new Set<string>([focused]);
			let frontier = [focused];
			for (let hop = 0; hop < focusDepth * 2 && frontier.length > 0; hop += 1) {
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
			},
		}));

		nodes = [...dsNodes, ...jobNodes];
		edges = derive.map((e) => ({
			id: `${e.target}->${e.source}`,
			source: e.target,
			target: e.source,
			animated: true,
			type: 'smoothstep',
		}));
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
		focused = focused === raw ? null : raw;
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
			<div class="focusbar">
				{#if focused}
					<span class="fname" title={focusedName}>{focusedName}</span>
					<span class="fsep">·</span>
				{:else}
					<span class="fhint">click a node to focus</span>
					<span class="fsep">·</span>
				{/if}
				<span class="flabel">depth</span>
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
					<button class="fclear" onclick={() => (focused = null)}>Clear</button>
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
</style>
