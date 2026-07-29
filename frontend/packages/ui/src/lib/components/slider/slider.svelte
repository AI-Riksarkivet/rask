<script lang="ts">
	import { Slider } from 'bits-ui';
	import { cn } from '../../utils/index.js';

	let {
		value = $bindable(50),
		min = 0,
		max = 100,
		step = 1,
		class: className,
		...rest
	}: {
		value?: number;
		min?: number;
		max?: number;
		step?: number;
		class?: string;
		'aria-label'?: string;
		/** Fires on user interaction only (not programmatic value changes) — lets a
		 *  controlled scrubber seek without a bind:value ↔ update loop. */
		onValueChange?: (value: number) => void;
	} = $props();
</script>

<Slider.Root
	type="single"
	bind:value
	{min}
	{max}
	{step}
	data-slot="slider"
	class={cn('relative flex w-full touch-none items-center select-none', className)}
	{...rest}
>
	{#snippet children({ thumbs })}
		<span class="bg-secondary relative h-1.5 w-full grow overflow-hidden rounded-full">
			<Slider.Range class="bg-primary absolute h-full" />
		</span>
		{#each thumbs as index (index)}
			<Slider.Thumb
				{index}
				class="border-primary bg-background focus-visible:ring-ring block size-4 rounded-full border-2 shadow transition-colors focus-visible:ring-2 focus-visible:outline-none"
			/>
		{/each}
	{/snippet}
</Slider.Root>
