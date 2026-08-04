<script lang="ts">
	/**
	 * The embedding atlas — the zone's real `AtlasMap`, bound to the dock's shared selection so a
	 * hit picked in the results panel highlights here, and a lasso here can seed the search.
	 */
	import AtlasMap from '$lib/atlas/AtlasMap.svelte';
	import { getBench } from '$lib/dock/bench.svelte';

	const bench = getBench();
</script>

<div class="h-full min-h-0">
	<AtlasMap
		bind:active={bench.active}
		onSelectionHits={(hits) => {
	if (hits.length > 0) bench.select(hits[0] ?? null);
}}
	/>
</div>
