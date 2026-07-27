<script lang="ts" module>
	export type RadioOption = { value: string; label: string; description?: string };
</script>

<script lang="ts">
	import { RadioGroup } from 'bits-ui';
	import { cn } from '../../utils/index.js';

	let {
		value = $bindable(''),
		options,
		class: className,
	}: { value?: string; options: RadioOption[]; class?: string } = $props();
</script>

<RadioGroup.Root bind:value data-slot="radio-group" class={cn('grid gap-1', className)}>
	{#each options as opt (opt.value)}
		<label
			class="hover:bg-secondary/40 flex cursor-pointer items-start gap-2 rounded p-1.5 transition-colors"
		>
			<RadioGroup.Item
				value={opt.value}
				class="border-border focus-visible:ring-ring data-[state=checked]:border-primary mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border transition-colors focus-visible:ring-2 focus-visible:outline-none"
			>
				{#snippet children({ checked })}
					{#if checked}<span class="bg-primary size-2 rounded-full"></span>{/if}
				{/snippet}
			</RadioGroup.Item>
			<span class="flex-1 text-xs">
				<span class="text-foreground font-medium">{opt.label}</span>
				{#if opt.description}
					<span class="text-muted-foreground block">{opt.description}</span>
				{/if}
			</span>
		</label>
	{/each}
</RadioGroup.Root>
