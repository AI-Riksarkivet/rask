<script lang="ts">
	import type { HTMLTdAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import type { WithElementRef } from '../../utils/types.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...rest
	}: WithElementRef<HTMLTdAttributes, HTMLTableCellElement> = $props();
</script>

<td
	bind:this={ref}
	data-slot="table-cell"
	class={cn(
	'p-2 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]',
	className,
)}
	{...rest}
>
	{@render children?.()}
</td>
