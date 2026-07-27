<script lang="ts">
	import { DropdownMenu as BitsDropdownMenu, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		...rest
	}: WithoutChild<BitsDropdownMenu.SeparatorProps> = $props();
</script>

<BitsDropdownMenu.Separator bind:ref class={cn('bg-border -mx-1 my-1 h-px', className)} {...rest} />
