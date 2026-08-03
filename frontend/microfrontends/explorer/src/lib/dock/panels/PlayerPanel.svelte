<script lang="ts">
	/**
	 * The media/transcript pane for whatever the dock currently has selected — the zone's real
	 * `PlayerPane`, no copy.
	 */
	import PlayerPane from '$lib/components/player-pane.svelte';
	import { getBench } from '$lib/dock/bench.svelte';

	const bench = getBench();
</script>

<div class="h-full min-h-0 overflow-auto">
	{#if bench.active === null}
		<p class="text-muted-foreground p-3 text-sm">Select a result to play it here.</p>
	{:else}
		<PlayerPane hit={bench.active} query={bench.spec.q} />
	{/if}
</div>
