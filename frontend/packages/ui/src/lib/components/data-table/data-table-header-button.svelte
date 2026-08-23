<script lang="ts">
	import { ChevronUp, ChevronDown, ChevronsUpDown } from '@lucide/svelte';
	import { cn } from '../../utils/cn.js';

	// The sortable column header for a tanstack column — wire it from a ColumnDef:
	// `header: ({ column }) => renderComponent(DataTableHeaderButton, { label: 'Name',
	//   sorted: column.getIsSorted(), onclick: column.getToggleSortingHandler() })`.
	// Same visual language as the plain SortHeader (chevrons, muted→foreground on active), but
	// context-free so tanstack owns the sort state.
	let {
		label,
		sorted = false,
		onclick,
		class: className,
	}: {
		label: string;
		/** The column's `getIsSorted()`: false, 'asc' or 'desc'. */
		sorted?: false | 'asc' | 'desc';
		onclick?: ((e: Event) => void) | undefined;
		class?: string;
	} = $props();
</script>

<button
	type="button"
	class={cn(
		'hover:text-foreground inline-flex items-center gap-1 transition-colors',
		sorted && 'text-foreground',
		className,
	)}
	{onclick}
>
	{label}
	{#if sorted === 'asc'}
		<ChevronUp class="size-3" />
	{:else if sorted === 'desc'}
		<ChevronDown class="size-3" />
	{:else}
		<ChevronsUpDown class="size-3 opacity-30" />
	{/if}
</button>
