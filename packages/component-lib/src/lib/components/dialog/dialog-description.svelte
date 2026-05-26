<script lang="ts">
	import { Dialog as BitsDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsDialog.DescriptionProps> = $props();
</script>

<BitsDialog.Description bind:ref class={cn('text-muted-foreground text-sm', className)} {...rest}>
	{@render children?.()}
</BitsDialog.Description>
