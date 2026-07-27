<script lang="ts">
	import HitCard from './hit-card.svelte';
	import type { Hit } from '@rask/media-api';
	import { hitKey, queryTerms, makeHighlighter } from '$lib/utils';

	type Props = {
		hits: Hit[];
		query?: string;
		active?: Hit | null;
		onselect?: (hit: Hit) => void;
		/** Status text shown when `hits` is empty (e.g. "Searching…", "No hits."). */
		emptyMessage?: string;
	};
	let {
		hits,
		query = '',
		active = null,
		onselect,
		emptyMessage = 'Enter a query above.',
	}: Props = $props();

	const activeKey = $derived(active ? hitKey(active) : null);

	// Compile the query → highlighter ONCE for the whole list, then hand the
	// prebuilt fn to every card. Previously each HitCard re-derived its own
	// RegExp; at large result/selection sizes that was up to ~1000 compilations.
	const highlight = $derived(makeHighlighter(queryTerms(query)));
</script>

<div>
	{#if hits.length === 0}
		<div class="text-muted-foreground px-4 py-6 text-sm">{emptyMessage}</div>
	{:else}
		{#each hits as hit (hitKey(hit))}
			<HitCard
				{hit}
				{query}
				{highlight}
				active={activeKey === hitKey(hit)}
				onclick={() => onselect?.(hit)}
			/>
		{/each}
	{/if}
</div>
