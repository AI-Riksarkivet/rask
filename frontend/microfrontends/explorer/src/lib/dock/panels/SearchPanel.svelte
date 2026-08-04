<script lang="ts">
	/**
	 * Results panel — the zone's REAL `SearchBar` + `HitList`, driving the dock's shared `Bench`.
	 * Selecting a hit here is what the atlas and the player panels follow.
	 */
	import SearchBar from '$lib/components/search-bar.svelte';
	import HitList from '$lib/components/hit-list.svelte';
	import { getBench } from '$lib/dock/bench.svelte';

	const bench = getBench();
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="border-border/60 border-b p-2">
		<SearchBar bind:spec={bench.spec} onsubmit={() => void bench.run()} />
	</div>
	{#if bench.error !== null}
		<p class="text-destructive p-3 text-sm">{bench.error}</p>
	{:else if bench.loading && bench.hits.length === 0}
		<p class="text-muted-foreground p-3 text-sm">Searching…</p>
	{:else}
		<div class="min-h-0 flex-1 overflow-auto">
			<HitList
				hits={bench.hits}
				query={bench.spec.q}
				active={bench.active}
				onselect={(h) => bench.select(h)}
			/>
		</div>
	{/if}
</div>
