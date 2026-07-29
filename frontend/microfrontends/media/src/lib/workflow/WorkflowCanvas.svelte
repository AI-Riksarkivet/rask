<script lang="ts">
	/** Thin wrapper: provides the Svelte Flow context (via <SvelteFlowProvider>)
	 *  so FlowPane can call useSvelteFlow() for drag-drop + context-menu
	 *  positioning. All the editor logic lives in FlowPane. */
	import { SvelteFlowProvider } from '@xyflow/svelte';
	import FlowPane from '$lib/workflow/FlowPane.svelte';
</script>

<div class="h-full w-full">
	<SvelteFlowProvider>
		<FlowPane />
	</SvelteFlowProvider>
</div>
