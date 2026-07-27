<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import type { WithElementRef } from '../../utils/types.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...rest
	}: WithElementRef<HTMLAttributes<HTMLElement>, HTMLTableCaptionElement> = $props();
</script>

<caption
	bind:this={ref}
	data-slot="table-caption"
	class={cn('text-muted-foreground mt-4 text-sm', className)}
	{...rest}
>
	{@render children?.()}
</caption>
