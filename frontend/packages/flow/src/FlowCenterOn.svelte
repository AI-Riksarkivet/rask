<script lang="ts">
	/**
	 * Bring one node into view, on demand.
	 *
	 * `fitView` frames the WHOLE graph, which is the wrong answer when the graph is large and the
	 * question is "where is this one". Marquez has the same affordance beside its zoom controls, and
	 * without it a canvas whose search and drawer can both re-root you leaves you having to find the
	 * node they selected — which on an estate-scale graph means panning until you spot it.
	 *
	 * Rendered INSIDE `<SvelteFlow>` so it can use the flow context (svelte-flow rule 5).
	 *
	 * DRIVEN BY A NONCE, not by the node id. Centring is a GESTURE — pressing the button again with
	 * the same node selected must move the viewport again — and an effect keyed on the id alone
	 * would fire once and then never, because nothing changed. The caller bumps `nonce`.
	 */
	import { useSvelteFlow } from '@xyflow/svelte';
	import { tick } from 'svelte';

	let {
		nodeId,
		nonce,
		zoom = 1,
		duration = 420,
	}: { nodeId: string | null; nonce: number; zoom?: number; duration?: number } = $props();

	const { fitView } = useSvelteFlow();

	$effect(() => {
		void nonce;
		const id = nodeId;
		if (!id) return;
		// `tick` first: a centring gesture usually follows a selection that has just changed the node
		// set, and framing a node the renderer has not placed yet centres on stale coordinates.
		// `fitView` over a single node is the documented way to centre — it also picks a sane zoom
		// rather than teleporting the viewport at whatever scale it happened to be at.
		void tick().then(() => fitView({ nodes: [{ id }], duration, maxZoom: zoom, minZoom: zoom }));
	});
</script>
