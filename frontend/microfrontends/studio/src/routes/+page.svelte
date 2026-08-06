<script lang="ts">
	/** Studio IS the flow builder — the node canvas is the zone root, not a page inside a launcher.
	 *  It had a launcher grid and an animation A/B demo beside it; both are gone, because a sandbox
	 *  zone whose landing is a menu of one real app spends its best surface on a redirect.
	 *
	 *  Three panes, and each owns a different question: the RAIL (shell `sidebarContent`) is the node
	 *  library — what can I add; the CANVAS is the graph — how is it wired; the RIGHT panel is the
	 *  inspector — what did the selected node actually produce. The split is resizable and its ratio
	 *  persists, so a user who works mostly in the canvas keeps their layout. */
	import { ResizableSplit } from '@rask/ui/resizable-split';
	import FlowsCanvas from '$lib/flows/FlowsCanvas.svelte';
	import FlowsInspector from '$lib/flows/FlowsInspector.svelte';
</script>

<svelte:head><title>Flows — Studio — RASK</title></svelte:head>

<div class="h-full min-h-0 w-full">
	<ResizableSplit storageKey="studio-flow-split" initial={0.72} minLeft={420} minRight={280}>
		{#snippet left()}
			<FlowsCanvas />
		{/snippet}
		{#snippet right()}
			<FlowsInspector />
		{/snippet}
	</ResizableSplit>
</div>
