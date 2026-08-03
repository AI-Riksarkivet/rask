<script lang="ts" module>
	import MedallionNode, { type MedallionNodeType } from './MedallionNode.svelte';
	import JobNode, { type JobNodeType } from './JobNode.svelte';
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
	 * polls itself, and the workbench owns ONE store shared across its panels, so the graph, the run
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
	import { Boxes, Cpu } from '@lucide/svelte';
	import { FlowAutoFit } from '@rask/flow';
	import type { LineageState } from './store.svelte';
	import { useColorMode } from '@rask/ui/color-mode';
	import { LAYER } from '@rask/api/lineage';
	import { depths, layout } from '@rask/flow';

	/**
	 * `base` and `navigate` arrive as PROPS rather than from `$app/paths` and `$app/navigation`.
	 *
	 * A package cannot import `$app/*` — the aliases only exist inside a SvelteKit app, and `@rask/ui`
	 * already establishes the rule (it imports none and detects the browser with
	 * `typeof window !== 'undefined'`). Handing the two zone-shaped values in is what lets this graph
	 * render in the lakehouse zone AND in the global workbench, which are served under different bases.
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

	// Which graph plane is shown — Datasets / Jobs (like Marquez). Columns is a first-class view
	// of its own (`/lineage/columns`); the sidebar's list views carry the rest of the old aside.
	let graphView = $state<'datasets' | 'jobs'>('datasets');

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

		if (graphView === 'jobs') {
			// Jobs plane (like Marquez's job lineage): one node per job; an edge producing-job →
			// consuming job whenever a job's input dataset was written by another job.
			const jobs = new Map<
				string,
				{
					author?: string | null;
					state?: string | null;
					inputs: Set<string>;
					outputs: Set<string>;
					failed: boolean;
				}
			>();
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
		const id = ev.node?.id ?? ev.targetNode?.id ?? null;
		if (!id) return;
		// Both targets live in THIS zone's lineage area (`/lakehouse/lineage/...`). The segment is
		// dynamic, so it is spelled out here rather than being derivable from the literal path.
		const kind = graphView === 'jobs' ? 'jobs' : 'datasets';
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
