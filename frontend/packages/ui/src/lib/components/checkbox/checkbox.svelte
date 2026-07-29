<script lang="ts">
	import { Checkbox as BitsCheckbox, type WithoutChildrenOrChild } from 'bits-ui';
	import CheckIcon from '@lucide/svelte/icons/check';
	import MinusIcon from '@lucide/svelte/icons/minus';
	import { cn } from '../../utils/cn.js';

	let {
		ref = $bindable(null),
		checked = $bindable(false),
		indeterminate = $bindable(false),
		class: className,
		...rest
	}: WithoutChildrenOrChild<BitsCheckbox.RootProps> = $props();
</script>

<BitsCheckbox.Root
	bind:ref
	bind:checked
	bind:indeterminate
	data-slot="checkbox"
	class={cn(
	'border-input dark:bg-input/30 data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground data-[state=checked]:border-primary focus-visible:border-ring focus-visible:ring-ring/50 aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive peer size-4 shrink-0 rounded-[4px] border shadow-xs transition-shadow outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50',
	className,
)}
	{...rest}
>
	{#snippet children({ checked, indeterminate })}
		<div
			data-slot="checkbox-indicator"
			class="flex size-full items-center justify-center text-current transition-none"
		>
			{#if indeterminate}
				<MinusIcon class="size-3.5" />
			{:else if checked}
				<CheckIcon class="size-3.5" />
			{/if}
		</div>
	{/snippet}
</BitsCheckbox.Root>
