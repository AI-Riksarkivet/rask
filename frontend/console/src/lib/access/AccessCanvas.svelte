<script lang="ts" module>
	import AccessGraphNode, { type AccessNodeType } from './AccessGraphNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule: register node components ONCE at module scope, never inline.
	const nodeTypes: NodeTypes = { access: AccessGraphNode };
</script>

<script lang="ts">
	// The access view's canvas: the graph the view composed and filtered, drawn with Svelte Flow, its
	// hover emphasis, the lit-hop badge and the legend that names the encodings. It owns the pointer
	// state; what the graph holds and which node is selected are the view's.
	import { type Edge, MarkerType } from '@xyflow/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { GraphCanvas } from '#lib/flow/index.ts';
	import { type BuiltGraph, layout } from './graph';

	type Props = {
		/** The graph to draw, already composed and faceted. */
		graph: BuiltGraph;
		/** Nodes before the facets: a canvas the facets emptied still draws, one with nothing does not. */
		total: number;
		/** The name each table and namespace object holds now, by its uuid id. */
		names: ReadonlyMap<string, string>;
		/** Glide the viewport to this node (see FlowFocus). */
		focus: { id: string } | null;
		/** Changes when the graph is re-framed: the view and the counts. */
		fitTrigger: string;
		/** The number of hops a query lit, or `null` when no query answered. */
		litHops: number | null;
		onnodeclick: (event: { node: { id: string } }) => void;
	};

	let { graph, total, names, focus, fitTrigger, litHops, onnodeclick }: Props = $props();

	/**
	 * The edges the pointer's subject touches — a node hover means "its edges", an edge hover means
	 * exactly that one.
	 *
	 * Hover is PURE ADDITION, by ruling: the touched edges get a subtle primary tint and nothing else
	 * on the canvas changes. Dimming everything unrelated reads as the UI breaking every time the
	 * pointer crosses the graph, and a missed pointerleave (fast exits over SVG edges lose the event)
	 * would leave the whole canvas stuck dark. Emphasis that only adds has nothing to get stuck:
	 * releasing the pointer just loses a tint, and the container-level reset below clears it even when
	 * the per-element leave event never fires.
	 */
	let hovered = $state<string | null>(null);
	/** Hovering an EDGE, not a node. A node answers "what does this touch"; an edge answers "what does
	 *  this one grant connect" — on a dense canvas those are different questions, and only the first
	 *  was answerable. One tuple IS an edge, so it deserves the same emphasis its endpoints get. */
	let hoveredEdge = $state<string | null>(null);
	const touchedEdges = $derived.by(() => {
		if (hoveredEdge) return new Set([hoveredEdge]);
		if (!hovered) return null;
		const edgeIds = new Set<string>();
		for (const e of graph.edges) {
			if (e.source === hovered || e.target === hovered) edgeIds.add(e.id);
		}
		return edgeIds;
	});

	/**
	 * Layout in its OWN derivation, so it re-runs only when the graph actually changes: the placer runs
	 * when facets, the query, or the view flip, never per pointer movement, which re-maps only the edge
	 * array.
	 */
	const positioned = $derived(layout(graph.nodes, graph.edges));

	// Nodes owe NOTHING to hover: roles come from the query and the facets alone — `dim` means
	// "filtered out or off-query", exactly what the legend says, and never "the pointer is elsewhere".
	// Writable deriveds: Svelte Flow writes a drag back through the binding, and the next change of the
	// graph replaces it.
	let nodes = $derived<AccessNodeType[]>(
		positioned.map((n) => ({
			id: n.id,
			type: 'access' as const,
			position: { x: n.x, y: n.y },
			data: {
				fgaId: n.id,
				fgaType: n.fgaType,
				label: names.get(n.id) ?? n.label,
				role: n.role,
				via: n.via,
			},
		})),
	);

	// Every edge gets an ARROWHEAD. A tuple is directional — `user:alice --owner--> table:x` — and an
	// undirected line in an authorization graph is not a simplification, it is the wrong picture:
	// "alice owns the table" and "the table owns alice" would render identically.
	//
	// The lit path is coloured EXPLICITLY here rather than through `--xy-edge-stroke-selected`. That
	// variable looks like the right lever and is not: the vendor stylesheet scopes it to
	// `.svelte-flow__edge.selected` / `:focus`, i.e. USER selection, which this never sets, so a lit hop
	// and a dimmed one would share a stroke colour. `--primary`/`--muted-foreground` are token `var()`s,
	// which resolve inside inline SVG styles and marker defs, so this stays on-token and flips with the
	// theme.
	let edges = $derived<Edge[]>(
		graph.edges.map((e) => ({
			id: e.id,
			source: e.source,
			target: e.target,
			label: e.condition ? `${e.label} ⏳` : e.label,
			animated: e.onPath,
			markerEnd: {
				type: MarkerType.ArrowClosed,
				width: 18,
				height: 18,
				// The arrowhead wears the same colour as its line — a green grant ending in a grey
				// arrow reads as two different edges meeting.
				color: e.onPath ? 'var(--primary)' : restingStroke(e.kind),
			},
			style: edgeStyle(e, touchedEdges?.has(e.id) ?? false),
		})),
	);

	/** Resting stroke per edge KIND — colour is the code (the legend names it): grants green,
	 *  role membership amber, `parent` scaffolding muted. Uncoded edges (the model view) keep
	 *  the muted look. */
	const KIND_STROKE: Record<string, string> = {
		grant: 'var(--success)',
		membership: 'var(--warning)',
		structural: 'var(--muted-foreground)',
	};
	function restingStroke(kind: string | undefined): string {
		return KIND_STROKE[kind ?? 'structural'] ?? 'var(--muted-foreground)';
	}

	/** Edge chrome: the query's lit path owns "lit" outright; hover ADDS a subtle primary tint to the
	 *  touched edges and takes nothing from the rest — there is no dimming pass, so releasing the
	 *  pointer has nothing to restore and a lost leave event has nothing to leave stuck. */
	function edgeStyle(
		e: { onPath: boolean; kind?: string; condition?: string | null },
		touched: boolean,
	): string {
		// A time-boxed grant is DASHED wherever it appears — "this expires" must stay legible whether
		// the edge is resting, tinted, or lit.
		const dash = e.condition ? ' stroke-dasharray: 6 4;' : '';
		if (e.onPath) return `stroke: var(--primary); stroke-width: 2;${dash}`;
		if (touched) return `stroke: var(--primary); stroke-width: 1.75; opacity: 0.75;${dash}`;
		const faint = e.kind === 'structural' || e.kind === undefined ? 0.35 : 0.5;
		return `stroke: ${restingStroke(e.kind)}; opacity: ${faint};${dash}`;
	}
</script>

<!-- Container-level reset: per-element pointerleave is lost on fast exits over SVG, and a
     hover emphasis that survives its own pointer is a bug regardless of how subtle it is.
     role="presentation": the handler is housekeeping on a layout wrapper, not interactivity —
     nothing here is focusable or announced. -->
<div
	role="presentation"
	class="relative min-w-0 flex-1 overflow-hidden rounded-md border border-border"
	onpointerleave={() => {
		hovered = null;
		hoveredEdge = null;
	}}
>
	{#if total === 0}
		<div
			class="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground"
		>
			Ask a question above to put the live graph on the canvas.
		</div>
	{:else}
		<GraphCanvas
			bind:nodes
			bind:edges
			{nodeTypes}
			{focus}
			{fitTrigger}
			fitPadding={0.2}
			fitMaxZoom={1.25}
			{onnodeclick}
			onnodepointerenter={(e: { node: { id: string } }) => (hovered = e.node.id)}
			onnodepointerleave={() => (hovered = null)}
			onedgepointerenter={(e: { edge: { id: string } }) => (hoveredEdge = e.edge.id)}
			onedgepointerleave={() => (hoveredEdge = null)}
		/>
	{/if}
	{#if litHops !== null}
		<Badge variant="secondary" class="absolute right-2 top-2 font-mono text-[10px]">
			{litHops} hop{litHops === 1 ? '' : 's'} lit
		</Badge>
	{/if}
	<!-- The encodings, named ON the canvas. Dashed-means-expiring and dimmed-means-filtered are
	     inventions of this view; a reader should not need the source code to decode them. One
	     line, muted, bottom-RIGHT — bottom-left belongs to Svelte Flow's Controls, and the two
	     overlap there. -->
	{#if total > 0}
		<div
			class="pointer-events-none absolute bottom-2 right-2 rounded bg-background/80 px-2 py-0.5 text-[10px] text-muted-foreground"
			data-slot="canvas-legend"
		>
			<span class="text-primary">━</span> derivation path ·
			<span class="text-success">━</span> grant ·
			<span class="text-warning">━</span> role ·
			<span>─</span> parent ·
			<span>┅ ⏳</span> time-boxed ·
			<span class="opacity-50">▢</span> dimmed = filtered out or off-query
		</div>
	{/if}
</div>
