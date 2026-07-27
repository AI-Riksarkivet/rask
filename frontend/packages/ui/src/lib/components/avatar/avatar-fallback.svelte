<script lang="ts">
	import { Avatar as BitsAvatar, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsAvatar.FallbackProps> = $props();
</script>

<BitsAvatar.Fallback
	bind:ref
	class={cn(
	'bg-sidebar-primary text-sidebar-primary-foreground flex size-full items-center justify-center rounded-md text-xs font-medium',
	className,
)}
	{...rest}
>
	{@render children?.()}
</BitsAvatar.Fallback>
