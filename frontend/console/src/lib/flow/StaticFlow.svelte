<script lang="ts">
	// SSR-able Svelte Flow diagram: explicit node dims + handle positions + a fixed
	// width/height so it renders WITHOUT a browser layout engine (the server-render
	// contract from the Svelte Flow SSR docs) — for a diagram drawn without a live canvas.
	import { SvelteFlow, Background } from '@xyflow/svelte';
	import type { Node, Edge, NodeTypes } from '@xyflow/svelte';

	let {
		nodes,
		edges,
		width = 900,
		height = 320,
		nodeTypes = undefined,
	}: {
		nodes: Node[];
		edges: Edge[];
		width?: number;
		height?: number;
		nodeTypes?: NodeTypes;
	} = $props();
</script>

<!-- The fit never shrinks the diagram below its natural size: a smaller zoom set its labels at 6-9px,
     too small to read. A diagram wider than its box is panned instead. -->
<SvelteFlow
	{nodes}
	{edges}
	{width}
	{height}
	{nodeTypes}
	fitView
	fitViewOptions={{ minZoom: 1, maxZoom: 1 }}
	minZoom={0.1}
	maxZoom={2}
>
	<Background />
</SvelteFlow>
