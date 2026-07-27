<script lang="ts">
	import { Avatar as BitsAvatar, type WithoutChildrenOrChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		...rest
	}: WithoutChildrenOrChild<BitsAvatar.ImageProps> = $props();
</script>

<BitsAvatar.Image bind:ref class={cn('aspect-square size-full', className)} {...rest} />
