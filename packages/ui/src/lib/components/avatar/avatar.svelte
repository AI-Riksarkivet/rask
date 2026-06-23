<script lang="ts">
	import { Avatar as BitsAvatar, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsAvatar.RootProps> = $props();
</script>

<BitsAvatar.Root
	bind:ref
	class={cn('relative flex size-8 shrink-0 overflow-hidden rounded-md', className)}
	{...rest}
>
	{@render children?.()}
</BitsAvatar.Root>
