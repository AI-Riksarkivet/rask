<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import type { WithElementRef } from '../../utils/types.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...rest
	}: WithElementRef<HTMLAttributes<HTMLTableSectionElement>, HTMLTableSectionElement> = $props();
</script>

<tfoot
	bind:this={ref}
	data-slot="table-footer"
	class={cn('bg-muted/50 border-t font-medium [&>tr]:last:border-b-0', className)}
	{...rest}
>
	{@render children?.()}
</tfoot>
