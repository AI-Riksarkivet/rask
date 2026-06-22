<script lang="ts">
	import { Dialog as BitsDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsDialog.TriggerProps> = $props();
</script>

<BitsDialog.Trigger bind:ref class={cn(className)} {...rest}>
	{@render children?.()}
</BitsDialog.Trigger>
