<script lang="ts">
	import { AlertDialog as BitsAlertDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsAlertDialog.TitleProps> = $props();
</script>

<BitsAlertDialog.Title
	bind:ref
	data-slot="alert-dialog-title"
	class={cn('text-lg leading-none font-semibold tracking-tight', className)}
	{...rest}
>
	{@render children?.()}
</BitsAlertDialog.Title>
