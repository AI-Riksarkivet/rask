<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '../../utils/index.js';

	// shadcn's DropdownMenuLabel is a plain styled <div>, NOT bits-ui GroupHeading
	// (GroupHeading requires a Menu.Group parent context — using it standalone in
	// Content throws "Context Menu.Group not found" and crashes the whole menu).
	let {
		class: className,
		ref = $bindable(null),
		inset,
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLDivElement>> & { inset?: boolean } = $props();
</script>

<div
	bind:this={ref}
	data-slot="dropdown-menu-label"
	data-inset={inset ? '' : undefined}
	class={cn('px-2 py-1.5 text-sm font-medium data-[inset]:pl-8', className)}
	{...restProps}
>
	{@render children?.()}
</div>
