<script lang="ts">
	import { AlertDialog as BitsAlertDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsAlertDialog.DescriptionProps> = $props();
</script>

<BitsAlertDialog.Description
	bind:ref
	data-slot="alert-dialog-description"
	class={cn('text-muted-foreground text-sm', className)}
	{...rest}
>
	{@render children?.()}
</BitsAlertDialog.Description>
