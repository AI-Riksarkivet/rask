<script lang="ts">
	import { Tabs as TabsPrimitive } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	// The tab ROOT. `min-h-0` alongside `flex flex-col`: a tab panel is very often a scroll container
	// (a queue, a members list), and a flex child's automatic minimum size is its content's — so
	// without this the panel grows past the root instead of scrolling inside it, exactly the failure
	// `card.svelte` documents on the horizontal axis.
	let {
		ref = $bindable(null),
		class: className,
		value = $bindable(),
		...restProps
	}: TabsPrimitive.RootProps = $props();
</script>

<TabsPrimitive.Root
	bind:ref
	bind:value
	data-slot="tabs"
	class={cn('flex min-h-0 flex-col gap-2', className)}
	{...restProps}
/>
