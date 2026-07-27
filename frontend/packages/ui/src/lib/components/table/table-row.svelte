<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import type { WithElementRef } from '../../utils/types.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...rest
	}: WithElementRef<HTMLAttributes<HTMLTableRowElement>, HTMLTableRowElement> = $props();
</script>

<tr
	bind:this={ref}
	data-slot="table-row"
	class={cn(
	'hover:bg-muted/50 data-[state=selected]:bg-muted border-b transition-colors',
	className,
)}
	{...rest}
>
	{@render children?.()}
</tr>
