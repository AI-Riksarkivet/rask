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
	import { Boxes, Cpu, Workflow } from '@lucide/svelte';
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

	// Which graph plane is shown. THREE, and the default is the interleaved one because it is the
	// only one that can show a dataset's producer:
	//
	//   pipeline — dataset → job → dataset, the DAG Marquez actually draws
	//   datasets — dataset → dataset (`derived_from`), the governed catalog's own projection
	//   jobs     — job → job, the run graph with the tables collapsed out
	//
	// The two single-kind planes are PROJECTIONS of the first, and each drops the edges that cross
	// the kind boundary. That is why the datasets plane reads as scattered islands on a real estate:
	// a table nothing derives FROM has no dataset→dataset edge to draw, so it is isolated there even
	// though a run produced it. Measured on the live estate: 51 dataset nodes, 18 edges, 25 isolated —
	// and every one of those 25 is attached to its producing run in the pipeline plane.
	//
	// Columns is a first-class view of its own (`/lineage/columns`); the sidebar's list views carry
	// the rest of the old aside.
	let graphView = $state<'pipeline' | 'datasets' | 'jobs'>('pipeline');

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
	function collectJobs(): { jobs: Map<string, JobAgg>; producedBy: Map<string, Set<string>> } {
		const jobs = new Map<string, JobAgg>();
		const producedBy = new Map<string, Set<string>>();
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
			for (const o of ev.outputs ?? []) {
				j.outputs.add(o);
				if (!producedBy.has(o)) producedBy.set(o, new Set());
				producedBy.get(o)!.add(ev.job);
			}
			for (const i of ev.inputs ?? []) j.inputs.add(i);
			jobs.set(ev.job, j);
		}
		return { jobs, producedBy };
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

	let nodes = $state.raw<FlowNode[]>([]);
	let edges = $state.raw<
		{ id: string; source: string; target: string; animated: boolean; type: string }[]
	>([]);
	// Re-fit the viewport only when the node-set or the view changes (not on every data poll).
	const fitKey = $derived(graphView + '|' + nodes.map((n) => n.id).join(','));

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

		if (graphView === 'pipeline') {
			// PIPELINE plane — the interleaved DAG: input dataset → job → output dataset.
			//
			// The layout maths is reused rather than reinvented. `depths`/`layout` read
			// DERIVATION-oriented edges (`source` derived from `target`, target one column left), so
			// emitting "the job derives from each input" and "each output derives from the job" makes
			// the two kinds alternate columns on their own — dataset d, job d+1, dataset d+2 — with no
			// separate interleaving pass. Rendering then reverses them, exactly as the datasets plane
			// does, so arrows read upstream → downstream.
			const { jobs } = collectJobs();
			const dsMeta = new Map(store.nodes.map((n) => [n.id, n]));

			// Every dataset the EVENTS name, not just the governed bulk read. An edge whose endpoint
			// is not in the node set is dropped by Svelte Flow silently, which would delete exactly
			// the connections this plane exists to draw.
			const dsSet = new Set<string>(dsMeta.keys());
			for (const j of jobs.values()) {
				for (const i of j.inputs) dsSet.add(i);
				for (const o of j.outputs) dsSet.add(o);
			}

			const derive: { source: string; target: string }[] = [];
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

			const ids = [...[...dsSet].map(dsId), ...[...jobs.keys()].map(jobId)];
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
						selected: false,
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
					selected: false,
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
			return;
		}

		if (graphView === 'jobs') {
			// Jobs plane (like Marquez's job lineage): one node per job; an edge producing-job →
			// consuming job whenever a job's input dataset was written by another job.
			const { jobs, producedBy } = collectJobs();
			const dsDepth = depths(
				store.nodes.map((n) => n.id),
				store.edges,
			);
			// A job's column = the deepest dataset it writes; source jobs (depth 0) pack into a grid.
			const jobLayer = new Map(
				[...jobs.entries()].map(([job, j]) => [
					job,
					Math.max(0, ...[...j.outputs].map((o) => dsDepth.get(o) ?? 0)),
				]),
			);
			const jobEdges = [...jobs.entries()].flatMap(([job, j]) =>
				[...j.inputs].flatMap((inp) =>
					[...(producedBy.get(inp) ?? [])]
						.filter((pj) => pj !== job)
						.map((pj) => ({ source: job, target: pj })),
				),
			);
			const place = layout([...jobs.keys()], jobEdges, (id) => jobLayer.get(id) ?? 0);
			nodes = [...jobs.entries()].map(([job, j]) => {
				return {
					id: job,
					type: 'job' as const,
					position: prev.get(job)?.position ?? place.get(job) ?? { x: 0, y: 0 },
					data: {
						id: job.replace(/^ray-jobs\//, ''),
						author: j.author,
						state: j.state,
						outputs: [...j.outputs],
						failed: j.failed,
						selected: false,
					},
				};
			});
			const seen = new Set<string>();
			const je: typeof edges = [];
			for (const [job, j] of jobs) {
				for (const inp of j.inputs) {
					for (const pj of producedBy.get(inp) ?? []) {
						if (pj === job || seen.has(`${pj}|${job}`)) continue;
						seen.add(`${pj}|${job}`);
						je.push({
							id: `${pj}->${job}`,
							source: pj,
							target: job,
							animated: true,
							type: 'smoothstep',
						});
					}
				}
			}
			edges = je;
			buildMs = Math.round((performance.now() - t0) * 10) / 10;
			return;
		}

		// Datasets plane (default): x = derivation depth (computed, so unrelated datasets never
		// stack on one overlapping column), with the depth-0 roots packed into a wrapped grid.
		const dsIds = store.nodes.map((n) => n.id);
		const dsDepth = depths(dsIds, store.edges);
		const place = layout(dsIds, store.edges, (id) => dsDepth.get(id) ?? 0);
		nodes = store.nodes.map((n) => {
			// Version/failed badges ride the bulk estate read's per-node rollup — no per-dataset
			// /producers fan-out (the run detail lives on the dataset detail page).
			const versions = n.versions ?? [];
			const failed = n.failed ?? false;
			const layer = dsDepth.get(n.id) ?? 0;
			return {
				id: n.id,
				type: 'medallion' as const,
				position: prev.get(n.id)?.position ?? place.get(n.id) ?? { x: 0, y: 0 },
				data: {
					id: n.id,
					// Color/icon: keep the medallion ramp for the known stage tables, else key by depth.
					layer: LAYER[n.id] ?? Math.min(layer, 4),
					source_uri: n.source_uri,
					tags: n.tags ?? [],
					versions,
					failed,
					selected: false,
					runState: runStateByDataset[n.id] ?? null,
				},
			};
		});
		edges = store.edges.map((e) => ({
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
		// Both targets live in THIS zone's lineage area (`/lakehouse/lineage/...`). The segment is
		// dynamic, so it is spelled out here rather than being derivable from the literal path.
		//
		// The pipeline plane holds BOTH kinds on one canvas, so the kind comes off the node id, not
		// off the active view — reading the view there would send every job click to a dataset page.
		// The two single-kind planes keep raw ids and fall through to the view.
		const [kind, id] = raw.startsWith(JOB_PREFIX)
			? (['jobs', raw.slice(JOB_PREFIX.length)] as const)
			: raw.startsWith(DATASET_PREFIX)
				? (['datasets', raw.slice(DATASET_PREFIX.length)] as const)
				: ([graphView === 'jobs' ? 'jobs' : 'datasets', raw] as const);
		navigate?.(`${base}/lineage/${kind}/${encodeURIComponent(id)}`);
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
			<div class="viewtoggle" role="tablist" aria-label="Graph view">
				<button
					class="vt"
					class:on={graphView === 'pipeline'}
					role="tab"
					aria-selected={graphView === 'pipeline'}
					onclick={() => (graphView = 'pipeline')}
				>
					<Workflow size={13} /> Pipeline
				</button>
				<button
					class="vt"
					class:on={graphView === 'datasets'}
					role="tab"
					aria-selected={graphView === 'datasets'}
					onclick={() => (graphView = 'datasets')}
				>
					<Boxes size={13} /> Datasets
				</button>
				<button
					class="vt"
					class:on={graphView === 'jobs'}
					role="tab"
					aria-selected={graphView === 'jobs'}
					onclick={() => (graphView = 'jobs')}
				>
					<Cpu size={13} /> Jobs
				</button>
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
	.viewtoggle {
		display: flex;
		gap: 2px;
		padding: 3px;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: 999px;
		box-shadow: var(--shadow);
	}
	.vt {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 4px 11px;
		border: none;
		background: transparent;
		color: var(--mut);
		font-size: 12px;
		font-weight: 600;
		border-radius: 999px;
		cursor: pointer;
		transition:
			color 0.2s var(--ease),
			background 0.2s var(--ease);
	}
	.vt:hover {
		color: var(--ink);
	}
	/* Tint from --primary, not --accent: --accent is a near-white surface token in the light
	   theme, so the selected pill was invisible there. */
	.vt.on {
		color: var(--primary);
		background: color-mix(in srgb, var(--primary) 14%, transparent);
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
