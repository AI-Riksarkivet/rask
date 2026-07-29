<script lang="ts">
	// Rendered INSIDE <SvelteFlow> so it can use the flow context (svelte-flow rule 5).
	// Re-frames the viewport whenever the node-set signature (`trigger`) changes — i.e. when nodes are
	// added/removed or the view switches — but NOT on every data poll, so it never fights a user pan/zoom.
	import { useSvelteFlow } from '@xyflow/svelte';
	import { tick } from 'svelte';

	let { trigger }: { trigger: string } = $props();
	const { fitView } = useSvelteFlow();

	$effect(() => {
		void trigger; // track the signature
		tick().then(() => fitView({ padding: 0.22, duration: 400 }));
	});
</script>
