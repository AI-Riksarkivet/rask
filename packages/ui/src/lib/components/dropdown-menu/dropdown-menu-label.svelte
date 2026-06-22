<script lang="ts">
	import { DropdownMenu as BitsDropdownMenu, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		inset,
		children,
		...rest
	}: WithoutChild<BitsDropdownMenu.GroupHeadingProps> & { inset?: boolean } = $props();
</script>

<BitsDropdownMenu.GroupHeading
	bind:ref
	data-inset={inset ? '' : undefined}
	class={cn('px-2 py-1.5 text-sm font-medium data-[inset]:pl-8', className)}
	{...rest}
>
	{@render children?.()}
</BitsDropdownMenu.GroupHeading>
