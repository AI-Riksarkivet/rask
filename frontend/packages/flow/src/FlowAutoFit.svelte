<script lang="ts">
	// Rendered INSIDE <SvelteFlow> so it can use the flow context (svelte-flow rule 5).
	// Re-frames the viewport whenever the node-set signature (`trigger`) changes — i.e. when nodes are
	// added/removed or the view switches — but NOT on every data poll, so it never fights a user pan/zoom.
	import { useSvelteFlow, type FitViewOptions } from '@xyflow/svelte';
	import { tick } from 'svelte';

	// `padding` is the caller's screen-space gutter (px per side) so the re-fit reserves the same
	// room for the floating overlays — toggle Panel, Controls, MiniMap — as the initial fitView.
	// `maxZoom` defaults to 1 — never scale a card ABOVE its natural size; a three-node estate used
	// to be blown up to fill the canvas, which read as "the graph is enormous". A caller whose canvas
	// is the page's main surface (the access explorer) may raise it a notch so a small graph does not
	// float in a sea of empty canvas.
	let {
		trigger,
		padding = 0.22,
		maxZoom = 1,
	}: { trigger: string; padding?: FitViewOptions['padding']; maxZoom?: number } = $props();
	const { fitView } = useSvelteFlow();

	$effect(() => {
		void trigger; // track the signature
		tick().then(() => fitView({ padding, duration: 400, maxZoom }));
	});
</script>
