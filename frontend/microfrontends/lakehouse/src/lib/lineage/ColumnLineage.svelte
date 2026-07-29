<script lang="ts" module>
	import ColumnNode, { type ColumnNodeType } from '$lib/lineage/ColumnNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline.
	const nodeTypes: NodeTypes = { column: ColumnNode };
</script>

<script lang="ts">
	import { untrack } from 'svelte';
	import { SvelteFlow, Background, BackgroundVariant, Controls } from '@xyflow/svelte';
	import { Columns3, ShieldAlert } from '@lucide/svelte';
	import { FlowAutoFit } from '@rask/panels/lineage';
	import { enter } from '@rask/ui/motion';
	import { useColorMode } from '@rask/ui/color-mode';
	import { ColumnLineageState } from '$lib/lineage/columns.svelte';
	import type { ColumnEdge, ColumnRef } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	// The dataset whose field-to-field subgraph is shown; the page owns the picker.
	let { dataset }: { dataset: string } = $props();

	const store = new ColumnLineageState();

	// Follow the estate theme live rather than pinning the canvas dark (see the graph page).
	const theme = useColorMode();

	// Switching datasets drops any open field panel — a field from the previous root has no place in the
	// new subgraph. This is a DATASET-change reset and nothing else: folding it into the live read below
	// would slam the user's open panel shut every time the estate changed.
	$effect(() => {
		void dataset;
		untrack(() => {
			store.selectedColumn = null;
		});
	});

	// The subgraph for the current dataset, live on the lineage cursor.
	liveRead(
		lineageTick,
		(name: string) => store.loadGraph(name),
		() => dataset,
	);

	// A focused field's provenance/impact, on the same cursor. Column lineage is derived from the very
	// events the cursor counts, so a re-read on any other schedule was guesswork.
	liveRead(
		lineageTick,
		(sel: { dataset: string; field: string } | null) => {
			if (sel) store.loadNeighbors(sel.dataset, sel.field);
		},
		() => store.selectedColumn,
	);

	let nodes = $state.raw<ColumnNodeType[]>([]);
	let edges = $state.raw<
		{
			id: string;
			source: string;
			target: string;
			animated: boolean;
			type: string;
			label?: string;
			class?: string;
		}[]
	>([]);
	/** Last layout-build cost (ms) — the perf readout the header chip shows. */
	let buildMs = $state(0);
	const fitKey = $derived(dataset + '|' + nodes.map((n) => n.id).join(','));

	// Rebuild the plane when the polled subgraph changes. Reconcile, don't rebuild: keep each
	// node's identity + dragged position across polls. Datasets are laid out left-to-right by
	// their DERIVATION DEPTH (computed from the field edges — no hardcoded name map, so foreign
	// datasets never stack on one x), columns stacked per dataset.
	$effect(() => {
		const cg = store.graph;
		const root = dataset;
		// Read current nodes UNTRACKED — only their last positions carry forward; tracking `nodes`
		// (the var reassigned below) would make this effect retrigger itself.
		const prev = new Map(untrack(() => nodes).map((node) => [node.id, node]));
		const t0 = performance.now();
		if (!cg) {
			nodes = [];
			edges = [];
			return;
		}
		const cols = cg.columns ?? [];
		const colEdges = cg.edges ?? [];
		const depth = datasetDepths(cols, colEdges);
		const masked = new Set(
			colEdges.filter((e) => e.masking).map((e) => `${e.target_dataset}::${e.target_field}`),
		);
		const perLayer: Record<number, number> = {};
		nodes = cols.map((c) => {
			const layer = depth.get(c.dataset) ?? 0;
			const row = (perLayer[layer] = (perLayer[layer] ?? 0) + 1) - 1;
			const id = `${c.dataset}::${c.field}`;
			return {
				id,
				type: 'column' as const,
				position: prev.get(id)?.position ?? { x: 20 + layer * 230, y: 24 + row * 76 },
				data: {
					dataset: c.dataset,
					field: c.field,
					type: c.type,
					layer,
					masked: masked.has(id),
					isRoot: c.dataset === root,
				},
			};
		});
		edges = colEdges.map((e) => {
			const s = `${e.source_dataset}::${e.source_field}`;
			const t = `${e.target_dataset}::${e.target_field}`;
			return {
				id: `${s}->${t}`,
				source: s,
				target: t,
				animated: true,
				type: 'smoothstep',
				label: e.transformation_subtype || e.transformation_type || '',
				class: e.masking ? 'masked-edge' : '',
			};
		});
		buildMs = Math.round((performance.now() - t0) * 10) / 10;
	});

	/** Per-dataset derivation depth from the field edges (source feeds target ⇒ target is one
	 * deeper), longest-path with an iteration cap so a cyclic payload can't loop forever. */
	function datasetDepths(cols: ColumnRef[], colEdges: ColumnEdge[]): Map<string, number> {
		const depth = new Map<string, number>();
		for (const c of cols) depth.set(c.dataset, 0);
		for (let i = 0; i < depth.size; i += 1) {
			let changed = false;
			for (const e of colEdges) {
				const next = (depth.get(e.source_dataset) ?? 0) + 1;
				if (e.source_dataset !== e.target_dataset && next > (depth.get(e.target_dataset) ?? 0)) {
					depth.set(e.target_dataset, next);
					changed = true;
				}
			}
			if (!changed) break;
		}
		return depth;
	}

	function selectNode(e: unknown) {
		const ev = e as { node?: { id: string }; targetNode?: { id: string } };
		const id = ev.node?.id ?? ev.targetNode?.id ?? null;
		if (!id) return;
		const sep = id.indexOf('::');
		if (sep < 0) return;
		store.selectedColumn = { dataset: id.slice(0, sep), field: id.slice(sep + 2) };
	}

	const focusedColumn = $derived(store.selectedColumn);
	const colUpstream = $derived(store.upstream?.related ?? []);
	const colDownstream = $derived(store.downstream?.related ?? []);
	const shortDs = (ds: string) => ds.split('$').at(-1) ?? ds;

	// The direct field-to-field edge between two columns (if the subgraph carries it), for its
	// transformation label + masking cue. Transitive (multi-hop) neighbors have no direct edge → no label.
	function edgeFor(src: ColumnRef, tgt: ColumnRef): ColumnEdge | undefined {
		return (store.graph?.edges ?? []).find(
			(e) =>
				e.source_dataset === src.dataset &&
				e.source_field === src.field &&
				e.target_dataset === tgt.dataset &&
				e.target_field === tgt.field,
		);
	}
	const transformLabel = (e: ColumnEdge | undefined) =>
		e ? e.transformation_subtype || e.transformation_type || 'derived' : '';
</script>

<div class="canvas">
	<SvelteFlow
		bind:nodes
		bind:edges
		{nodeTypes}
		colorMode={theme.current}
		fitView
		onnodeclick={selectNode}
	>
		<Background variant={BackgroundVariant.Dots} gap={16} />
		<Controls />
		<FlowAutoFit trigger={fitKey} />
	</SvelteFlow>
	<span class="perf mono" title="last layout build">{buildMs}ms</span>

	{#if (store.graph?.columns?.length ?? 0) === 0}
		<div class="empty">
			<b>No field lineage for {dataset} yet.</b><br />
			Column-level edges appear when a producing run emits the <code>columnLineage</code> facet.
		</div>
	{/if}

	{#if focusedColumn}
		<!-- Field-level provenance/impact (#24): click a column node → its direct upstream (what it
		     was derived from) and downstream (what derives from it), each with the transformation
		     kind and, for a masking derivation, the same red PII cue the masked edges use. -->
		<div class="field-panel" {@attach enter({ y: 6 })}>
			<div class="fp-head">
				<div class="fp-title">
					<Columns3 size={13} />
					<span class="mono fp-field">{focusedColumn.field}</span>
				</div>
				<button
					class="fp-close"
					aria-label="Close field panel"
					onclick={() => (store.selectedColumn = null)}>×</button
				>
			</div>
			<div class="fp-ds mono">{focusedColumn.dataset}</div>

			<div class="fp-group">
				<span class="rel-label">Provenance · derived from</span>
				{#if colUpstream.length}
					<ul class="fp-list">
						{#each colUpstream as r (r.dataset + '::' + r.field)}
							{@const e = edgeFor(r, focusedColumn)}
							<li class="fp-row" class:masked={e?.masking}>
								<button
									class="fp-col mono"
									onclick={() => (store.selectedColumn = { dataset: r.dataset, field: r.field })}
								>
									<span class="fp-col-field">{r.field}</span>
									<span class="fp-col-ds">{shortDs(r.dataset)}</span>
								</button>
								{#if transformLabel(e)}
									<span class="fp-xf" class:masked={e?.masking}>{transformLabel(e)}</span>
								{/if}
								{#if e?.masking}<ShieldAlert size={12} class="fp-mask-ic" />{/if}
							</li>
						{/each}
					</ul>
				{:else}
					<p class="hint fp-none">No upstream fields — a source column.</p>
				{/if}
			</div>

			<div class="fp-group">
				<span class="rel-label">Impact · feeds</span>
				{#if colDownstream.length}
					<ul class="fp-list">
						{#each colDownstream as r (r.dataset + '::' + r.field)}
							{@const e = edgeFor(focusedColumn, r)}
							<li class="fp-row" class:masked={e?.masking}>
								<button
									class="fp-col mono"
									onclick={() => (store.selectedColumn = { dataset: r.dataset, field: r.field })}
								>
									<span class="fp-col-field">{r.field}</span>
									<span class="fp-col-ds">{shortDs(r.dataset)}</span>
								</button>
								{#if transformLabel(e)}
									<span class="fp-xf" class:masked={e?.masking}>{transformLabel(e)}</span>
								{/if}
								{#if e?.masking}<ShieldAlert size={12} class="fp-mask-ic" />{/if}
							</li>
						{/each}
					</ul>
				{:else}
					<p class="hint fp-none">No downstream fields — nothing derives from this column.</p>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.canvas {
		position: relative;
		flex: 1 1 0;
		min-height: 0;
	}
	/* A masking column derivation (e.g. a PII hash) reads as a red dashed edge. */
	:global(.masked-edge .svelte-flow__edge-path) {
		stroke: var(--fail);
		stroke-dasharray: 5 3;
	}
	.perf {
		position: absolute;
		bottom: 8px;
		right: 10px;
		z-index: 5;
		color: var(--faint);
		font-size: 10px;
	}
	.empty {
		position: absolute;
		top: 18px;
		left: 18px;
		color: var(--mut);
		font-size: 13px;
		line-height: 1.7;
	}
	.empty code {
		color: var(--ink);
		background: #0c1018;
		padding: 0 4px;
		border-radius: 4px;
	}
	.hint {
		color: var(--mut);
		font-size: 12px;
	}
	.rel-label {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		color: var(--mut);
	}

	/* ---- the clicked field's provenance/impact panel (#24) ---- */
	.field-panel {
		position: absolute;
		top: 14px;
		right: 14px;
		width: 250px;
		max-height: calc(100% - 28px);
		overflow: auto;
		z-index: 5;
		padding: 10px 12px;
		border: 1px solid var(--line);
		border-radius: var(--radius);
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
		box-shadow: var(--shadow);
		font-size: 12px;
	}
	.fp-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	.fp-title {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
	}
	.fp-field {
		font-weight: 600;
		font-size: 13px;
		color: var(--ink);
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.fp-close {
		flex: 0 0 auto;
		border: none;
		background: transparent;
		color: var(--mut);
		font-size: 16px;
		line-height: 1;
		cursor: pointer;
		padding: 0 2px;
	}
	.fp-close:hover {
		color: var(--ink);
	}
	.fp-ds {
		font-size: 10.5px;
		color: #6f86a6;
		word-break: break-all;
		margin: 1px 0 8px;
	}
	.fp-group {
		margin-bottom: 10px;
	}
	.fp-group .rel-label {
		display: block;
		margin-bottom: 5px;
	}
	.fp-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.fp-row {
		display: flex;
		align-items: center;
		gap: 5px;
		padding-left: 7px;
		border-left: 2px solid var(--line-2);
	}
	/* Same red PII cue the masked derivation edges use. */
	.fp-row.masked {
		border-left-color: var(--fail);
	}
	.fp-col {
		display: flex;
		align-items: baseline;
		gap: 6px;
		flex: 1;
		min-width: 0;
		padding: 3px 6px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel);
		color: var(--ink);
		cursor: pointer;
		text-align: left;
		transition:
			border-color 0.2s var(--ease),
			background 0.2s var(--ease);
	}
	.fp-col:hover {
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 12%, transparent);
	}
	.fp-col-field {
		font-size: 11.5px;
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.fp-col-ds {
		font-size: 9.5px;
		color: var(--mut);
		margin-left: auto;
	}
	.fp-xf {
		flex: 0 0 auto;
		font-size: 9.5px;
		color: var(--mut);
		text-transform: uppercase;
		letter-spacing: 0.3px;
	}
	.fp-xf.masked {
		color: var(--fail);
	}
	:global(.fp-mask-ic) {
		flex: 0 0 auto;
		color: var(--fail);
	}
	.fp-none {
		margin: 0;
		font-style: italic;
	}
</style>
