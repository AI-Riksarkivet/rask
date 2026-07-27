<script lang="ts">
	import { Progress as BitsProgress, type WithoutChildrenOrChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		ref = $bindable(null),
		class: className,
		value,
		max = 100,
		...rest
	}: WithoutChildrenOrChild<BitsProgress.RootProps> = $props();
</script>

<BitsProgress.Root
	bind:ref
	{value}
	{max}
	data-slot="progress"
	class={cn('bg-primary/20 relative h-2 w-full overflow-hidden rounded-full', className)}
	{...rest}
>
	<div
		data-slot="progress-indicator"
		class="bg-primary h-full w-full flex-1 transition-all"
		style:transform={`translateX(-${100 - (100 * (value ?? 0)) / (max ?? 1)}%)`}
	></div>
</BitsProgress.Root>
