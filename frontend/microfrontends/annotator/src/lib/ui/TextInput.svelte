<script lang="ts">
	// The zone's text field. It is a deliberate MIRROR of @rask/ui's input component (same token
	// classes, same h-8 / rounded-lg / ring-3 geometry) because @rask/ui does not expose `./input`
	// in its package exports yet — only the data-table's filter wrapper reaches it. Keeping the
	// mirror here, in one file, is why every field in the annotator can look like a data/admin
	// field without each call site hand-rolling border+ring classes. When @rask/ui exports its
	// input, delete this file and re-point the four importers at it.
	import type { HTMLInputAttributes } from 'svelte/elements';
	import { cn } from '@rask/ui/utils';

	let {
		value = $bindable(''),
		class: className,
		...rest
	}: HTMLInputAttributes & { value?: string } = $props();
</script>

<input
	bind:value
	data-slot="input"
	class={cn(
		'dark:bg-input/30 border-input focus-visible:border-ring focus-visible:ring-ring/50 placeholder:text-muted-foreground h-8 w-full min-w-0 rounded-lg border bg-transparent px-2.5 py-1 text-sm transition-colors outline-none focus-visible:ring-3 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
		className,
	)}
	{...rest}
/>
