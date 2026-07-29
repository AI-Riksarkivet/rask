<script lang="ts">
	import { Search } from '@lucide/svelte';
	import { Input } from '../input/index.js';
	import { cn } from '../../utils/cn.js';

	// The text-filter input for a table toolbar. Bind `value` and feed it to tanstack as the
	// global filter (`state: { get globalFilter() { return value } }`, with getFilteredRowModel())
	// or filter rows yourself before `data` — the component only owns the input chrome.
	let {
		value = $bindable(''),
		placeholder = 'Filter…',
		class: className,
	}: {
		value?: string;
		placeholder?: string;
		class?: string;
	} = $props();
</script>

<div class={cn('relative max-w-sm', className)}>
	<Search
		class="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2"
	/>
	<Input type="search" bind:value {placeholder} class="pl-8" />
</div>
