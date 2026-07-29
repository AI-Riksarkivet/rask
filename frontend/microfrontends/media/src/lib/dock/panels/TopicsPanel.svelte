<script lang="ts">
	/**
	 * Chunks for whichever topic the treemap panel last selected, playable in place.
	 *
	 * The empty state is not a placeholder — it is the panel's resting state, and a docked panel is
	 * visible even when nothing is selected (unlike the `/tree` route's right pane, which only exists
	 * because the split always mounts it). So it says what to do rather than sitting blank.
	 */
	import TopicResultsPanel from '$lib/components/topic-results-panel.svelte';
	import { getMediaWorkbench } from '$lib/dock/context';

	const workbench = getMediaWorkbench();
</script>

<div class="h-full w-full">
	{#if workbench.selectedTopic}
		<TopicResultsPanel
			topic={workbench.selectedTopic}
			onClose={() => (workbench.selectedTopic = null)}
		/>
	{:else}
		<div class="text-muted-foreground grid h-full place-items-center p-6 text-center text-xs">
			Click a topic in the Treemap panel to browse its chunks here.
		</div>
	{/if}
</div>
