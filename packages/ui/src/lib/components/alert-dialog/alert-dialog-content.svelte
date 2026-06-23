<script lang="ts">
	import { AlertDialog as BitsAlertDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';
	import AlertDialogOverlay from './alert-dialog-overlay.svelte';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsAlertDialog.ContentProps> = $props();
</script>

<BitsAlertDialog.Portal>
	<AlertDialogOverlay />
	<BitsAlertDialog.Content
		bind:ref
		data-slot="alert-dialog-content"
		class={cn(
			'border-border bg-background fixed top-1/2 left-1/2 z-50 grid w-full max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4 rounded-lg border p-6 shadow-lg',
			'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
			className,
		)}
		{...rest}
	>
		{@render children?.()}
	</BitsAlertDialog.Content>
</BitsAlertDialog.Portal>
