<script lang="ts">
	import { Tabs as TabsPrimitive } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	// `min-h-0` for the same reason as the root: a panel that holds a scrolling list must be allowed
	// to shrink below its content, or the list pushes the page instead of scrolling itself.
	let {
		ref = $bindable(null),
		class: className,
		...restProps
	}: TabsPrimitive.ContentProps = $props();
</script>

<TabsPrimitive.Content
	bind:ref
	data-slot="tabs-content"
	class={cn('min-h-0 flex-1 outline-none', className)}
	{...restProps}
/>
