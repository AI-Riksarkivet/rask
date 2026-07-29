<script lang="ts">
	// SSR-able Svelte Flow diagram: explicit node dims + handle positions + a fixed
	// width/height so it renders WITHOUT a browser layout engine (the server-render
	// contract from the Svelte Flow SSR docs). Used by src/routes/diagram/+server.ts
	// to emit a static HTML pipeline diagram (docs / OG images / non-JS).
	import { SvelteFlow, Background } from '@xyflow/svelte';
	import type { Node, Edge } from '@xyflow/svelte';

	let {
		nodes,
		edges,
		width = 900,
		height = 320,
	}: { nodes: Node[]; edges: Edge[]; width?: number; height?: number } = $props();
</script>

<SvelteFlow {nodes} {edges} {width} {height} fitView minZoom={0.1} maxZoom={2}>
	<Background />
</SvelteFlow>
