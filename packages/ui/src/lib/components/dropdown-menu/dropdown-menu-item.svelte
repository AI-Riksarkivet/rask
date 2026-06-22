<script lang="ts">
	import { DropdownMenu as BitsDropdownMenu, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		inset,
		variant = 'default',
		children,
		...rest
	}: WithoutChild<BitsDropdownMenu.ItemProps> & {
		inset?: boolean;
		variant?: 'default' | 'destructive';
	} = $props();
</script>

<BitsDropdownMenu.Item
	bind:ref
	data-inset={inset ? '' : undefined}
	data-variant={variant}
	class={cn(
		'data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground data-[variant=destructive]:text-destructive data-[variant=destructive]:data-[highlighted]:bg-destructive/10 relative flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 data-[inset]:pl-8 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0',
		className,
	)}
	{...rest}
>
	{@render children?.()}
</BitsDropdownMenu.Item>
