<script lang="ts" module>
	import ColumnNode, { type ColumnNodeType } from '$lib/lineage/ColumnNode.svelte';
	import DatasetGroup, { type DatasetGroupType } from '$lib/lineage/DatasetGroup.svelte';
	import { leafSegment } from '@rask/api/identifiers';
	import { ElbowEdge } from '@rask/flow';
	import type { EdgeTypes, NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline.
	const nodeTypes: NodeTypes = { column: ColumnNode, dsgroup: DatasetGroup };

	/** The container id for a table. PREFIXED so it can never collide with a column id, which is
	 *  `${dataset}::${field}` and therefore already contains the dataset name. */
	const groupId = (dataset: string) => `group:${dataset}`;
	/** Room at the top of a container for its title strip, and the inset around its columns — the
	 *  same numbers handed to ELK, so the pre-layout paint and the laid-out one agree. */
	const GROUP_LABEL = 26;
	const GROUP_PAD = 12;
	// Same routed edge the table graph uses — a field-level graph has the same long cross-layer edges
	// and the same reason not to draw them through whatever sits between.
	const edgeTypes: EdgeTypes = { elbow: ElbowEdge };
</script>

<script lang="ts">
	import { untrack } from 'svelte';
	import { SvelteFlow, Background, BackgroundVariant, Controls, MiniMap } from '@xyflow/svelte';
	import { Columns3, ShieldAlert } from '@lucide/svelte';
	import { elkLayout, FlowAutoFit, routeKey } from '@rask/flow';
	import type { ElkRoute } from '@rask/flow';
	import { enter } from '@rask/ui/motion';
	import { useColorMode } from '@rask/ui/color-mode';
	import { ColumnLineageState } from '$lib/lineage/columns.svelte';
	import type { ColumnEdge, ColumnRef } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	/**
	 * The dataset whose field-to-field subgraph is shown; the page owns the picker.
	 *
	 * `selectedColumn` is BINDABLE so the chosen field can live in the URL beside the dataset that is
	 * already there. Selecting a field is what the whole provenance panel hangs off, and it was the
	 * one thing on this page a reload threw away — you could share "the columns of this table" but
	 * never "how THIS field was derived", which is the question someone actually links to. Spelled as
	 * one `dataset::field` string because the panel can walk you to a field of a DIFFERENT table, so
	 * the selection is not always the dataset in the picker.
	 */
	let {
		dataset,
		selectedColumn = $bindable(null),
	}: { dataset: string; selectedColumn?: string | null } = $props();

	const store = new ColumnLineageState();

	// Applied at INIT rather than in an effect: this is the incoming URL value, and running it as an
	// effect would re-apply it every time the binding changed and fight the user's next click.
	if (selectedColumn) {
		const sep = selectedColumn.indexOf('::');
		if (sep > 0) {
			store.selectedColumn = {
				dataset: selectedColumn.slice(0, sep),
				field: selectedColumn.slice(sep + 2),
			};
		}
	}

	// Mirror the selection back out. `untrack` on the WRITE side keeps this one-way: the effect reads
	// the store and writes the prop, never the reverse.
	$effect(() => {
		const sc = store.selectedColumn;
		untrack(() => {
			selectedColumn = sc ? `${sc.dataset}::${sc.field}` : null;
		});
	});

	/**
	 * The column under the pointer, and the derivation chain it belongs to.
	 *
	 * A field-level graph is dense — every table contributes all its columns — so "which of these
	 * lines is the one I care about" is the standing question, and following a single thread by eye
	 * across a screen of parallel edges is exactly what people cannot do. Hovering lights the whole
	 * chain, both directions, transitively.
	 */
	let hovered = $state<string | null>(null);

	/**
	 * How far out the field graph is read. The table graph has had a depth control since it shipped;
	 * this one was pinned at one hop with no way to ask for more, so "where did this column actually
	 * come from" was unanswerable here whenever the answer was a table away — which, in a medallion
	 * cascade, is most of the time.
	 *
	 * Changing it RE-READS rather than re-filtering: the extra hop is not in the payload to filter.
	 */
	const COLUMN_DEPTHS = [1, 2, 3];
	function setDepth(d: number): void {
		if (store.depth === d) return;
		store.depth = d;
		void store.loadGraph(dataset);
	}

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

	let nodes = $state.raw<(ColumnNodeType | DatasetGroupType)[]>([]);
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

	/**
	 * ELK bookkeeping, mirroring the table-level graph (`LineageGraph.svelte`) exactly.
	 *
	 * This canvas placed nodes by ARITHMETIC — `x = 20 + layer * 230, y = 24 + row * 76` — which is
	 * not a layout: a column was never pulled level with the column it feeds, and rows were assigned
	 * in iteration order, so edges crossed for no reason. Marquez lays its column graph out with the
	 * SAME ELK its table graph uses (`web/libs/graph/src/layout/useLayout.ts` is shared by both
	 * routes); this zone had ELK on one graph and arithmetic on the other.
	 *
	 * `lastElkShape` is the same memoisation guard and exists for the same reason: without it a poll
	 * re-runs ELK and overwrites a node the user had DRAGGED.
	 */
	let buildGeneration = 0;
	let lastElkShape = '';

	/** The routes ELK computed for the current layout, keyed by the derivation pair — see the table
	 *  graph for why they live beside the edges rather than inside them. */
	let elkRoutes = $state.raw<Map<string, ElkRoute>>(new Map());
	/** The real card box (`ColumnNode.svelte` is `width: 170px`), so ELK reserves what is drawn
	 *  rather than a guess — the one place the table graph still hands ELK less than it knows. */
	const CARD = { width: 170, height: 52 };
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
		// UNTRACKED: this effect assigns `elkRoutes` when a layout resolves, and a tracked read would
		// make it depend on its own output. The `.then` patches the live edges directly.
		const knownRoutes = untrack(() => elkRoutes);
		if (!cg) {
			nodes = [];
			edges = [];
			return;
		}
		const cols = cg.columns ?? [];
		const colEdges = cg.edges ?? [];
		// Read INSIDE the build so hover re-runs it. That is deliberate: the shape guard below means a
		// hover never re-lays out, so this costs one node/edge rebuild — the same work a poll does,
		// and `buildMs` in the corner reports it honestly if it ever stops being cheap.
		const chain = chainOf(hovered, colEdges);
		const depth = datasetDepths(cols, colEdges);
		const masked = new Set(
			colEdges.filter((e) => e.masking).map((e) => `${e.target_dataset}::${e.target_field}`),
		);
		/**
		 * ONE CONTAINER PER TABLE, and the columns nested inside it.
		 *
		 * The flat version stacked columns by DERIVATION LAYER, and two tables at the same layer
		 * therefore shared a column — measured on `acme-silver$features`: one stack at x=20 holding
		 * fields of `acme-silver$features` and `acme-bronze$events` interleaved, separable only by
		 * reading each subtitle. Nesting makes that shape unrepresentable rather than merely unlikely.
		 */
		const datasets = [...new Set(cols.map((c) => c.dataset))].sort(
			(a, b) => (depth.get(a) ?? 0) - (depth.get(b) ?? 0),
		);
		const perDataset: Record<string, number> = {};
		const colNodes: ColumnNodeType[] = cols.map((c) => {
			const layer = depth.get(c.dataset) ?? 0;
			const row = (perDataset[c.dataset] = (perDataset[c.dataset] ?? 0) + 1) - 1;
			const id = `${c.dataset}::${c.field}`;
			return {
				id,
				type: 'column' as const,
				parentId: groupId(c.dataset),
				// Clamped to its table's box: a column dragged out of its container would assert a
				// membership that is not true, and this graph's entire subject is membership.
				extent: 'parent' as const,
				// PARENT-RELATIVE, which is what `parentId` means to Svelte Flow and what ELK returns
				// for a nested child — so the pre-layout paint and the laid-out one use one convention.
				position: prev.get(id)?.position ?? {
					x: GROUP_PAD,
					y: GROUP_LABEL + GROUP_PAD + row * (CARD.height + GROUP_PAD),
				},
				data: {
					dataset: c.dataset,
					field: c.field,
					type: c.type,
					layer,
					masked: masked.has(id),
					isRoot: c.dataset === root,
					dimmed: chain !== null && !chain.has(id),
				},
			};
		});
		const groupNodes: DatasetGroupType[] = datasets.map((ds, i) => {
			const count = cols.filter((c) => c.dataset === ds).length;
			const previous = prev.get(groupId(ds));
			return {
				id: groupId(ds),
				type: 'dsgroup' as const,
				position: previous?.position ?? { x: 20 + i * (CARD.width + 90), y: 20 },
				// EXPLICIT dimensions: `extent: 'parent'` has nothing to clamp against until the
				// parent has a box, and a group node has no intrinsic size of its own.
				width: previous?.width ?? CARD.width + GROUP_PAD * 2,
				height:
					previous?.height ??
					GROUP_LABEL + GROUP_PAD * 2 + count * (CARD.height + GROUP_PAD) - GROUP_PAD,
				data: { dataset: ds, isRoot: ds === root, count },
			};
		});
		// PARENTS FIRST. Svelte Flow resolves `parentId` by array order and drops a child that
		// appears before its parent — the whole graph would render empty.
		nodes = [...groupNodes, ...colNodes];
		// DERIVATION-oriented for ELK: a target field is derived FROM a source field, and `elkLayout`
		// reverses its input so the drawn graph reads upstream-on-the-left. Passing the wire
		// orientation straight through would mirror the whole picture.
		const derive = colEdges.map((e) => ({
			source: `${e.target_dataset}::${e.target_field}`,
			target: `${e.source_dataset}::${e.source_field}`,
		}));
		// Built from `cols`, NOT from `nodes`. Reading `nodes` here is a TRACKED read of the very
		// state this effect assigns, which self-triggers it — `effect_update_depth_exceeded`, and
		// the canvas rendered its nodes but never its edges. Same id expression as the node build
		// above, so the two cannot drift.
		const ids = cols.map((c) => `${c.dataset}::${c.field}`);
		// A LOOKUP, not a split on '::'. Dataset names carry their own punctuation (`ns$table`) and a
		// field could too; recovering the owner by parsing the composite id is a guess, and the
		// answer is right here in the payload.
		const datasetOfColumn = new Map(cols.map((c) => [`${c.dataset}::${c.field}`, c.dataset]));
		// SORTED on BOTH halves. Sorting only the edges left this order-SENSITIVE, and the feed does
		// not promise a stable order: a poll that returned the identical graph in a different
		// order read as a shape change, re-ran ELK and snapped a dragged node back. Measured —
		// the drag reverted across a poll whose node and edge counts were byte-identical.
		const shape = `${[...ids].sort().join(',')}|${derive
			.map((e) => `${e.source}>${e.target}`)
			.sort()
			.join(',')}`;
		if (shape !== lastElkShape) {
			lastElkShape = shape;
			const generation = ++buildGeneration;
			void elkLayout(ids, derive, {
				size: () => CARD,
				parentOf: (id) => {
					const ds = datasetOfColumn.get(id);
					return ds === undefined ? undefined : groupId(ds);
				},
				groupPadding: GROUP_PAD,
				groupLabelHeight: GROUP_LABEL,
			})
				.then((elk) => {
					if (generation !== buildGeneration || elk.nodes.size === 0) return;
					untrack(() => {
						nodes = nodes.map((n) => {
							// A container is placed AND SIZED by ELK, which sizes it around the children
							// it just laid out — the reason the two are one pass rather than a box drawn
							// around a finished layout.
							const box = elk.groups.get(n.id);
							if (box) {
								return {
									...n,
									position: { x: box.x, y: box.y },
									width: box.width,
									height: box.height,
								};
							}
							const pos = elk.nodes.get(n.id);
							return pos ? { ...n, position: pos } : n;
						});
						// The routes ELK computed for THIS layout, handed to the edges below. Kept in
						// state rather than threaded through the node map because the edge list is
						// rebuilt on every data tick while a layout happens only on a shape change.
						elkRoutes = elk.routes;
						edges = edges.map((e) => {
							const route = elk.routes.get(routeKey(e.target, e.source));
							return route ? { ...e, data: { route } } : e;
						});
					});
				})
				.catch(() => {
					// The arithmetic placement above is already on screen and readable, so a failed
					// layout degrades rather than blanking the canvas. Reset so the next tick retries.
					lastElkShape = '';
				});
		}

		edges = colEdges.map((e) => {
			const s = `${e.source_dataset}::${e.source_field}`;
			const t = `${e.target_dataset}::${e.target_field}`;
			return {
				id: `${s}->${t}`,
				source: s,
				target: t,
				animated: true,
				type: 'elbow',
				data: { route: knownRoutes.get(routeKey(t, s)) },
				label: e.transformation_subtype || e.transformation_type || '',
				// Both cues on one attribute: masking is a property of the edge, dimming is a property
				// of what is hovered, and an edge can be either, both or neither.
				class: [
					e.masking ? 'masked-edge' : '',
					chain && !(chain.has(s) && chain.has(t)) ? 'dim-edge' : '',
				]
					.filter(Boolean)
					.join(' '),
			};
		});
		buildMs = Math.round((performance.now() - t0) * 10) / 10;
	});

	/**
	 * Every column reachable from `id` along derivation edges, in BOTH directions, transitively —
	 * `null` when nothing is hovered, which is the "no highlight" state rather than an empty chain.
	 *
	 * Both directions because the question a hover answers is "what is this field's story", and half
	 * of that story is upstream. `seen` is checked before enqueueing, so a cyclic payload terminates.
	 */
	function chainOf(id: string | null, colEdges: ColumnEdge[]): Set<string> | null {
		if (!id) return null;
		const adjacent = new Map<string, string[]>();
		const link = (a: string, b: string) => {
			(adjacent.get(a) ?? adjacent.set(a, []).get(a)!).push(b);
		};
		for (const e of colEdges) {
			const from = `${e.source_dataset}::${e.source_field}`;
			const to = `${e.target_dataset}::${e.target_field}`;
			link(from, to);
			link(to, from);
		}
		const seen = new Set([id]);
		const queue = [id];
		while (queue.length > 0) {
			const next = queue.shift();
			if (next === undefined) break;
			for (const n of adjacent.get(next) ?? []) {
				if (seen.has(n)) continue;
				seen.add(n);
				queue.push(n);
			}
		}
		return seen;
	}

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

	/** Hovering a CONTAINER is not hovering a column — it has no chain, and treating it as one would
	 *  dim the entire graph the moment the pointer crossed a table's title strip. */
	function hoverNode(e: unknown) {
		const ev = e as { node?: { id: string }; targetNode?: { id: string } };
		const id = ev.node?.id ?? ev.targetNode?.id ?? null;
		hovered = id && id.includes('::') ? id : null;
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
	const shortDs = (ds: string) => leafSegment(ds);

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
		{edgeTypes}
		colorMode={theme.current}
		fitView
		onnodeclick={selectNode}
		onnodepointerenter={hoverNode}
		onnodepointerleave={() => (hovered = null)}
	>
		<Background variant={BackgroundVariant.Dots} gap={16} />
		<Controls />
		<!-- The table graph has had one since it shipped; this canvas did not, and it is the denser of
		     the two. Marquez shows a minimap on its column view as well (`ZoomPanSvg.tsx`), which an
		     earlier revision of the parity audit recorded as a match — it was not. -->
		<MiniMap
			pannable
			zoomable
			position="bottom-right"
			width={140}
			height={96}
			bgColor="var(--panel)"
			nodeColor="var(--primary)"
			nodeStrokeColor="var(--line)"
			maskColor="color-mix(in srgb, var(--panel-2) 72%, transparent)"
		/>
		<FlowAutoFit trigger={fitKey} />
	</SvelteFlow>
	<div class="depthbar">
		<span
			class="dlabel"
			title="how many tables out to read. One hop is this dataset and whatever directly feeds or reads its columns; each further hop follows the lineage one table further."
			>hops</span
		>
		{#each COLUMN_DEPTHS as d (d)}
			<button
				class="db"
				class:on={store.depth === d}
				aria-pressed={store.depth === d}
				onclick={() => setDepth(d)}
			>
				{d}
			</button>
		{/each}
	</div>
	<span class="perf mono" title="last layout build">{buildMs}ms</span>

	{#if !store.settled}
		<div class="empty"><b>Loading field lineage for {dataset}…</b></div>
	{:else if !store.online}
		<div class="empty">
			<b>Could not read field lineage for {dataset}.</b><br />
			The lineage service did not answer — this is a read failure, not a statement that the dataset has
			no column-level edges.
		</div>
	{:else if (store.graph?.columns?.length ?? 0) === 0}
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
	/* Off the hovered chain. Faded rather than removed, and the LABEL fades with the line — a
	   transformation name left at full contrast over a ghosted edge reads as the one to look at. */
	:global(.dim-edge .svelte-flow__edge-path),
	:global(.dim-edge .svelte-flow__edge-text),
	:global(.dim-edge .svelte-flow__edge-textbg) {
		opacity: 0.12;
	}
	/* Top-left, above the canvas chrome — the same corner the table graph puts its controls in, so a
	   reader moving between the two views looks in one place. */
	.depthbar {
		position: absolute;
		top: 10px;
		left: 10px;
		z-index: 5;
		display: flex;
		align-items: center;
		gap: 3px;
		padding: 3px 6px;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: color-mix(in srgb, var(--panel) 92%, transparent);
		backdrop-filter: blur(6px);
	}
	.dlabel {
		font-size: 10px;
		color: var(--mut);
		margin-right: 2px;
		cursor: help;
	}
	.db {
		min-width: 20px;
		padding: 2px 5px;
		border: none;
		border-radius: 5px;
		background: transparent;
		color: var(--mut);
		font: inherit;
		font-size: 11px;
		cursor: pointer;
	}
	.db:hover {
		background: color-mix(in srgb, var(--ink) 8%, transparent);
		color: var(--ink);
	}
	.db.on {
		color: var(--primary);
		background: color-mix(in srgb, var(--primary) 14%, transparent);
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
