<script lang="ts">
	import { AlertDialog as BitsAlertDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';
	import { buttonVariants } from '../button/index.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsAlertDialog.ActionProps> = $props();
</script>

<BitsAlertDialog.Action
	bind:ref
	data-slot="alert-dialog-action"
	class={cn(buttonVariants(), className)}
	{...rest}
>
	{@render children?.()}
</BitsAlertDialog.Action>
