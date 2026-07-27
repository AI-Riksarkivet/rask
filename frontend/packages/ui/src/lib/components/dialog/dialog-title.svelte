<script lang="ts">
	import { Dialog as BitsDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsDialog.TitleProps> = $props();
</script>

<BitsDialog.Title
	bind:ref
	class={cn('text-lg leading-none font-semibold tracking-tight', className)}
	{...rest}
>
	{@render children?.()}
</BitsDialog.Title>
