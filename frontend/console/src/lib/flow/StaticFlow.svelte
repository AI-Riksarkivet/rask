<script lang="ts">
	// A static Svelte Flow diagram: explicit node dims + handle positions + a fixed
	// width/height, so it is drawn whole at its first paint with nothing measured first —
	// for a diagram drawn without a live canvas.
	import { SvelteFlow, Background } from '@xyflow/svelte';
	import type { Node, Edge, NodeTypes } from '@xyflow/svelte';
	import { appearance } from '@riksarkivet/ui/appearance';

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

	// Svelte Flow stamps its `colorMode` on its root as a class, `light` when none is given, and the
	// design system declares its light token set on `.light` as well as on the root: an unset mode
	// paints every token inside the canvas light while text inherits the page's colour. The canvas
	// therefore carries the page's own mode.
	const colorMode = $derived(appearance.dark ? 'dark' : 'light');
</script>

<!-- The fit never shrinks the diagram below its natural size: a smaller zoom set its labels at 6-9px,
     too small to read. A diagram wider than its box is panned instead. -->
<SvelteFlow
	{nodes}
	{edges}
	{width}
	{height}
	{nodeTypes}
	{colorMode}
	fitView
	fitViewOptions={{ minZoom: 1, maxZoom: 1 }}
	minZoom={0.1}
	maxZoom={2}
>
	<Background />
</SvelteFlow>
