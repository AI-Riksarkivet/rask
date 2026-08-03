<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts" module>
	import ColumnNode, { type ColumnNodeType } from '../lineage/ColumnNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline. The node
	// component is the PAGE's own ColumnNode — imported, never copied, so a column tile in the
	// workbench and on /lineage/columns can never drift apart.
	const nodeTypes: NodeTypes = { column: ColumnNode };
</script>

<script lang="ts">
	/**
	 * `<rask-lakehouse-columns>` — field-to-field lineage, VISUALLY IDENTICAL to the zone's
	 * /lineage/columns page: the same @rask/ui Select picker, the same xyflow canvas over the same
	 * ColumnNode tiles laid out by the same derivation-depth maths, the same red masking cue on a
	 * PII derivation, and the same per-field provenance/impact panel on click.
	 *
	 * It cannot mount the page's own `ColumnLineage.svelte`: that component reads through
	 * `$lib/live/tick.svelte` → `feeds.remote`, and an element bundle has neither `$app/*` nor
	 * remote functions. The subgraph, the dataset list and the per-field neighbors are therefore
	 * read through the SHARED lineage client (./store — root-absolute `/lakehouse/api/*`, session
	 * cookie riding, so it answers from any zone's page). The xyflow + @rask/flow stylesheets are
	 * injected once by the elements entry (see index.ts).
	 *
	 * The mount stamp + poll counter stay as the no-remount witness; a column node dispatches the
	 * rask:select contract event instead of deep-linking (a panel has no router).
	 */
	import { untrack } from 'svelte';
	import { SvelteFlow, Background, BackgroundVariant, Controls } from '@xyflow/svelte';
	import { Columns3, ShieldAlert } from '@lucide/svelte';
	import { FlowAutoFit } from '@rask/flow';
	import { useColorMode } from '@rask/ui/color-mode';
	import { Select } from '@rask/ui/select';
	import type { ColumnEdge, ColumnGraph, ColumnRef, DatasetSummary } from '@rask/api/lineage';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { ElementPoll } from './poll.svelte';
	import { client } from './store';

	let { pollms = 30000, dataset = '' }: { pollms?: number; dataset?: string } = $props();

	// The panel's own dataset. `dataset` is the PROPERTY-DOWN half of the cross-zone contract (the
	// page's `?dataset=` analogue); the in-panel picker owns it afterwards, so the property is only
	// honoured when it actually changes — untracked, or the assignment would re-enter. It is NOT the
	// initialiser (`$state(dataset)` reads the prop once and never again → state_referenced_locally);
	// the effect below adopts the property's first value on mount just as it adopts any later one.
	let selected = $state('');
	$effect(() => {
		const incoming = dataset;
		untrack(() => {
			if (incoming && incoming !== selected) selected = incoming;
		});
	});

	// Follow the estate theme live rather than pinning the canvas dark (see the graph page).
	const theme = useColorMode();

	type ColumnsPayload = { datasets: DatasetSummary[]; graph: ColumnGraph | null };

	const poll = new ElementPoll<ColumnsPayload>(async () => {
		const res = await client.listDatasets();
		if (!res.ok)
			throw new Error(
				`datasets: ${res.status === 401 ? 'session expired or no access' : `HTTP ${res.status}`}`,
			);
		const name = selected;
		const graph = name ? await client.fetchColumnGraph(name) : null;
		if (name && graph === null) throw new Error(`no column lineage read for ${name}`);
		return { datasets: res.data.datasets ?? [], graph };
	});
	// The chosen dataset is PART of the read, so picking one must re-arm the loop (an immediate
	// tick) rather than leave the canvas empty until the interval comes round.
	$effect(() => {
		void selected;
		return poll.start(pollms);
	});

	const datasets = $derived(poll.data?.datasets ?? []);
	const options = $derived(datasets.map((d) => ({ value: d.name, label: d.name })));
	// Latest-wins, the element's answer to the page's `{#key selected}`: a subgraph rooted at the
	// PREVIOUS dataset must never render under the new pick while its read is in flight.
	const graph = $derived(poll.data?.graph?.root === selected ? (poll.data?.graph ?? null) : null);

	// ---- the focused field's provenance/impact (the page's field panel) ----
	let focused = $state<{ dataset: string; field: string } | null>(null);
	let upstream = $state<ColumnRef[] | null>(null);
	let downstream = $state<ColumnRef[] | null>(null);
	/** Monotonic request id — latest-wins, so a slow earlier field never lands under a newer one. */
	let fieldReq = 0;

	async function loadNeighbors(ds: string, field: string): Promise<void> {
		const req = (fieldReq += 1);
		// Clear FIRST: stale neighbors must not render under a new field's header.
		upstream = null;
		downstream = null;
		const [up, down] = await Promise.all([
			client.fetchColumnUpstream(ds, field),
			client.fetchColumnDownstream(ds, field),
		]);
		if (req !== fieldReq) return;
		upstream = up?.related ?? [];
		downstream = down?.related ?? [];
	}

	// Switching datasets drops any open field panel — a field from the previous root has no place in
	// the new subgraph. A DATASET change only: folding it into the poll would slam the panel shut
	// every time the estate moved.
	$effect(() => {
		void selected;
		untrack(() => {
			focused = null;
			upstream = null;
			downstream = null;
		});
	});

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
	/** Last layout-build cost (ms) — the perf readout the page's canvas shows. */
	let buildMs = $state(0);
	const fitKey = $derived(selected + '|' + nodes.map((n) => n.id).join(','));

	// Mirrored from lib/lineage/ColumnLineage.svelte — reconcile, don't rebuild: each node keeps its
	// identity and dragged position across polls. Datasets are laid out left-to-right by their
	// DERIVATION DEPTH (computed from the field edges — no hardcoded name map), columns stacked.
	$effect(() => {
		const cg = graph;
		const root = selected;
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
	function datasetDepths(cols: { dataset: string }[], colEdges: ColumnEdge[]): Map<string, number> {
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

	const shortDs = (ds: string) => ds.split('$').at(-1) ?? ds;

	// The direct field-to-field edge between two columns (if the subgraph carries it), for its
	// transformation label + masking cue. Transitive neighbors have no direct edge → no label.
	function edgeFor(
		src: { dataset: string; field: string },
		tgt: { dataset: string; field: string },
	): ColumnEdge | undefined {
		return (graph?.edges ?? []).find(
			(e) =>
				e.source_dataset === src.dataset &&
				e.source_field === src.field &&
				e.target_dataset === tgt.dataset &&
				e.target_field === tgt.field,
		);
	}
	const transformLabel = (e: ColumnEdge | undefined) =>
		e ? e.transformation_subtype || e.transformation_type || 'derived' : '';

	// Read through a `const` (the page's shape too): the markup narrows `focused` inside `{#if}`, and
	// a mutable `let` loses that narrowing inside the each-block bodies the compiler emits.
	const focusedColumn = $derived(focused);

	/** The canvas host — the event target the contract dispatch bubbles from (a flow node is not a
	 *  DOM node this component owns a reference to). */
	let host = $state<HTMLElement | null>(null);

	function focus(ds: string, field: string): void {
		focused = { dataset: ds, field };
		void loadNeighbors(ds, field);
		host?.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-columns',
					kind: 'lineage-column',
					id: `${ds}::${field}`,
					label: `${field} · ${shortDs(ds)}`,
				} satisfies SelectDetail,
			}),
		);
	}

	function selectNode(e: unknown) {
		const ev = e as { node?: { id: string }; targetNode?: { id: string } };
		const id = ev.node?.id ?? ev.targetNode?.id ?? null;
		if (!id) return;
		const sep = id.indexOf('::');
		if (sep < 0) return;
		focus(id.slice(0, sep), id.slice(sep + 2));
	}
</script>

<div class="app bg-background">
	<header>
		<div class="head-text">
			<h1>Columns <span class="sub">field-to-field lineage</span></h1>
			<p class="text-muted-foreground mb-2 text-[11px]">
				mounted {poll.mountedAt} · poll #{poll.polls}
			</p>
		</div>
		<div class="picker">
			<Select bind:value={selected} ariaLabel="Dataset" placeholder="pick a dataset…" {options} />
		</div>
	</header>

	{#if poll.error !== null}
		<p class="text-destructive p-3 text-sm">Column lineage unavailable: {poll.error}</p>
	{:else if !selected}
		<p class="text-muted-foreground p-3 text-sm">
			Pick a dataset above to see how its fields were derived.
		</p>
	{:else}
		<div class="canvas" bind:this={host}>
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
			<span class="perf" title="last layout build">{buildMs}ms</span>

			{#if (graph?.columns?.length ?? 0) === 0}
				<div class="empty">
					<b>No field lineage for {selected} yet.</b><br />
					Column-level edges appear when a producing run emits the <code>columnLineage</code> facet.
				</div>
			{/if}

			{#if focusedColumn}
				<!-- Field-level provenance/impact: click a column node → its direct upstream (what it was
				     derived from) and downstream (what derives from it), each with the transformation kind
				     and, for a masking derivation, the same red PII cue the masked edges use. -->
				<div class="field-panel">
					<div class="fp-head">
						<div class="fp-title">
							<Columns3 size={13} />
							<span class="fp-field">{focusedColumn.field}</span>
						</div>
						<button class="fp-close" aria-label="Close field panel" onclick={() => (focused = null)}
							>×</button
						>
					</div>
					<div class="fp-ds">{focusedColumn.dataset}</div>

					<div class="fp-group">
						<span class="rel-label">Provenance · derived from</span>
						{#if upstream === null}
							<p class="hint fp-none">Loading…</p>
						{:else if upstream.length}
							<ul class="fp-list">
								{#each upstream as r (r.dataset + '::' + r.field)}
									{@const e = edgeFor(r, focusedColumn)}
									<li class="fp-row" class:masked={e?.masking}>
										<button class="fp-col" onclick={() => focus(r.dataset, r.field)}>
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
						{#if downstream === null}
							<p class="hint fp-none">Loading…</p>
						{:else if downstream.length}
							<ul class="fp-list">
								{#each downstream as r (r.dataset + '::' + r.field)}
									{@const e = edgeFor(focusedColumn, r)}
									<li class="fp-row" class:masked={e?.masking}>
										<button class="fp-col" onclick={() => focus(r.dataset, r.field)}>
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
	{/if}
</div>

<style>
	/* Mirrored from routes/lineage/columns/+page.svelte + lib/lineage/ColumnLineage.svelte — the page
	   styles the canvas with the legacy palette bridge (--line/--panel/--mut/--fail), which resolves
	   through the host page's :root exactly as it does in the zone. */
	.app {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
	}
	header {
		display: flex;
		align-items: center;
		gap: 16px;
		flex-wrap: wrap;
		padding: 8px 12px 2px;
		border-bottom: 1px solid var(--line);
		background: linear-gradient(180deg, var(--panel-2), transparent);
	}
	h1 {
		font-size: 14px;
		margin: 0;
		font-weight: 600;
	}
	.sub {
		color: var(--mut);
		font-size: 11px;
		font-weight: 400;
	}
	.picker {
		margin-left: auto;
		min-width: 200px;
	}
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
		font-family: ui-monospace, monospace;
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
		background: var(--panel-2);
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

	/* ---- the clicked field's provenance/impact panel ---- */
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
		font-family: ui-monospace, monospace;
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
		font-family: ui-monospace, monospace;
		font-size: 10.5px;
		color: var(--mut);
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
		font-family: ui-monospace, monospace;
		cursor: pointer;
		text-align: left;
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
