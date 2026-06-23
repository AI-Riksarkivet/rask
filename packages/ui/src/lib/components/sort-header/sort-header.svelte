<script lang="ts">
	import { ChevronUp, ChevronDown, ChevronsUpDown } from '@lucide/svelte';

	type SortDir = 'asc' | 'desc';
	let {
		label,
		col,
		sortKey,
		sortDir,
		onsort,
		class: className = '',
	}: {
		label: string;
		col: string;
		sortKey: string;
		sortDir: SortDir;
		onsort: (col: string) => void;
		class?: string;
	} = $props();

	const active = $derived(sortKey === col);
</script>

<th class="text-muted-foreground px-3 py-2 font-medium {className}">
	<button
		class="hover:text-foreground inline-flex items-center gap-1 transition-colors {active
			? 'text-foreground'
			: ''}"
		onclick={() => onsort(col)}
	>
		{label}
		{#if active}
			{#if sortDir === 'asc'}
				<ChevronUp class="h-3 w-3" />
			{:else}
				<ChevronDown class="h-3 w-3" />
			{/if}
		{:else}
			<ChevronsUpDown class="h-3 w-3 opacity-30" />
		{/if}
	</button>
</th>
