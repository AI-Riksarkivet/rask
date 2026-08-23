<script lang="ts">
	import { Tabs as TabsPrimitive } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	// `w-fit`, not `w-full`: a three-tab strip stretched across a wide column reads as a segmented
	// control that owns the page. These tabs switch a PANEL, so the strip stays the width of its
	// labels and the panel below it owns the column.
	let { ref = $bindable(null), class: className, ...restProps }: TabsPrimitive.ListProps = $props();
</script>

<TabsPrimitive.List
	bind:ref
	data-slot="tabs-list"
	class={cn(
		'bg-muted text-muted-foreground inline-flex h-9 w-fit items-center justify-center rounded-lg p-[3px]',
		className,
	)}
	{...restProps}
/>
