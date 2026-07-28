<script lang="ts">
	/**
	 * The topic hierarchy as a zoomable treemap.
	 *
	 * Clicking a topic writes `selectedTopic` on the shared workbench state rather than calling a prop
	 * callback: the results panel is a SIBLING mounted into a different DOM subtree with no component
	 * relationship, so there is no prop to pass. On the `/tree` route both live inside one
	 * `ResizableSplit`, which is exactly the nesting a dock dissolves.
	 */
	import { onMount } from 'svelte';
	import TopicTreemap from '$lib/components/topic-treemap.svelte';
	import { getMediaWorkbench } from '$lib/dock/context';

	const workbench = getMediaWorkbench();

	// Safe to call from several panels at once — `load()` de-duplicates the in-flight read.
	onMount(() => {
		void workbench.load();
	});
</script>

<div class="h-full w-full">
	{#if workbench.phase === 'unavailable'}
		<div class="text-muted-foreground grid h-full place-items-center p-6 text-center text-sm">
			<div>
				<p class="text-foreground mb-1 font-medium">No topics yet</p>
				<p>
					Run <code class="bg-muted rounded-md px-1 py-0.5">ratch feature topics</code> to cluster the chunks
					into a topic hierarchy.
				</p>
			</div>
		</div>
	{:else if workbench.phase === 'error'}
		<div class="text-destructive grid h-full place-items-center p-6 text-center text-sm">
			Failed to load topics: {workbench.errorMsg}
		</div>
	{:else if workbench.phase === 'ready' && workbench.hierarchy}
		<!-- Re-bind the narrowed value: the {#if} guard's narrowing does not flow into a snippet body. -->
		{@const tree = workbench.hierarchy}
		<TopicTreemap
			hierarchy={tree}
			noiseLabel={workbench.noiseLabel}
			onShowResults={(name) => (workbench.selectedTopic = name)}
		/>
	{:else}
		<div class="text-muted-foreground grid h-full place-items-center text-sm">Loading…</div>
	{/if}
</div>
