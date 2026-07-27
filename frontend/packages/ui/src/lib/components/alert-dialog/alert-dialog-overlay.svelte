<script lang="ts">
	import { AlertDialog as BitsAlertDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		...rest
	}: WithoutChild<BitsAlertDialog.OverlayProps> = $props();
</script>

<BitsAlertDialog.Overlay
	bind:ref
	data-slot="alert-dialog-overlay"
	class={cn(
		'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 fixed inset-0 z-50 bg-black/50',
		className,
	)}
	{...rest}
/>
