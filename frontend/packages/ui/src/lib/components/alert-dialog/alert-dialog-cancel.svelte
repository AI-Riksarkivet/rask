<script lang="ts">
	import { AlertDialog as BitsAlertDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';
	import { buttonVariants } from '../button/index.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsAlertDialog.CancelProps> = $props();
</script>

<BitsAlertDialog.Cancel
	bind:ref
	data-slot="alert-dialog-cancel"
	class={cn(buttonVariants({ variant: 'outline' }), className)}
	{...rest}
>
	{@render children?.()}
</BitsAlertDialog.Cancel>
