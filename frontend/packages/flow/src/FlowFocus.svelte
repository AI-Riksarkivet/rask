<script lang="ts">
	// Rendered INSIDE <SvelteFlow> so it can use the flow context (svelte-flow rule 5).
	// Glides the viewport to ONE node when `target` changes — the "jump to" affordance. The prop is an
	// object, not a bare id, so clicking the same chip twice re-centres (a fresh object re-fires the
	// effect; a string equal to the last one would not, and a jump that only works once reads as broken).
	import { useSvelteFlow, type FitViewOptions } from '@xyflow/svelte';
	import { tick } from 'svelte';

	let {
		target,
		padding = 0.22,
	}: { target: { id: string } | null; padding?: FitViewOptions['padding'] } = $props();
	const { fitView } = useSvelteFlow();

	$effect(() => {
		const t = target;
		if (!t) return;
		// maxZoom above FlowAutoFit's 1: a single card centred at natural size can sit in a sea of
		// canvas; a slight zoom-in says "this one" without the blown-up look the auto-fit cap prevents.
		tick().then(() => fitView({ nodes: [{ id: t.id }], padding, duration: 350, maxZoom: 1.1 }));
	});
</script>
