<script lang="ts">
	/** Workflow page — node-based editor (left) + a persistent Inspector (right).
	 *  Self-contained; fills the layout's content slot. The whole app is
	 *  client-only (ssr=false), so Svelte Flow needs no browser guard. */
	import { ResizableSplit } from '@rask/ui/resizable-split';
	import WorkflowCanvas from '$lib/workflow/WorkflowCanvas.svelte';
	import WorkflowInspector from '$lib/workflow/WorkflowInspector.svelte';
	import CommandMenu from '$lib/workflow/CommandMenu.svelte';
	import { graph } from '$lib/workflow/graph.svelte';
</script>

<svelte:window
	onkeydown={(e) => {
	if (e.key === 'Escape' && graph.selectedHit) graph.closeDetail();
}}
/>

<CommandMenu />

<div class="h-full min-h-0 w-full">
	<ResizableSplit
		storageKey="lance-media-workflow-split"
		initial={0.7}
		minLeft={460}
		minRight={300}
	>
		{#snippet left()}
			<WorkflowCanvas />
		{/snippet}
		{#snippet right()}
			<WorkflowInspector />
		{/snippet}
	</ResizableSplit>
</div>
