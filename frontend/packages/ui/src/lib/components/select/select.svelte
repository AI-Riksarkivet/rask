<script lang="ts" module>
	/** A row for the Select. Exported because consumers type their own options array with it —
	 *  keeping it private is what sent them to a second Select in another package. */
	export interface SelectOption {
		value: string;
		label: string;
		disabled?: boolean;
	}
</script>

<script lang="ts">
	// The design-system single-select, built on the bits-ui@2 headless primitive (keyboard nav, typeahead,
	// focus + portal positioning come from bits-ui). It keeps the exact API @rask/ui's Select exposed
	// (value/options/placeholder/ariaLabel/disabled) so the migrated apps/web components port by swapping only
	// the import. The listbox is PORTALLED — only the open select's options mount — which is what the e2e
	// drives (getByLabel(trigger) to open, then getByRole('option')). Styled with the shadcn design tokens.
	import { Check, ChevronDown } from '@lucide/svelte';
	import { Select } from 'bits-ui';

	let {
		value = $bindable(''),
		options,
		placeholder = 'Select…',
		ariaLabel,
		disabled = false,
	}: {
		value?: string;
		options: SelectOption[];
		placeholder?: string;
		ariaLabel?: string;
		disabled?: boolean;
	} = $props();

	const label = $derived(options.find((o) => o.value === value)?.label ?? placeholder);
</script>

<Select.Root type="single" bind:value items={options} {disabled}>
	<Select.Trigger
		aria-label={ariaLabel}
		data-slot="select-trigger"
		class="border-input bg-background hover:bg-accent/40 focus-visible:border-ring focus-visible:ring-ring/50 inline-flex h-8 min-w-30 items-center justify-between gap-2 rounded-md border px-2.5 text-sm focus-visible:ring-3 focus-visible:outline-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50"
	>
		<span class={value ? '' : 'text-muted-foreground'}>{label}</span>
		<ChevronDown size={13} class="opacity-60" />
	</Select.Trigger>
	<Select.Portal>
		<Select.Content
			sideOffset={4}
			data-slot="select-content"
			class="bg-popover text-popover-foreground z-50 max-h-64 overflow-y-auto rounded-md border p-1 shadow-md"
		>
			<Select.Viewport>
				{#each options as o (o.value)}
					<Select.Item
						value={o.value}
						label={o.label}
						disabled={o.disabled}
						class="data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground flex cursor-default items-center justify-between gap-2 rounded-sm px-2 py-1 text-sm select-none data-[disabled]:opacity-40"
					>
						{#snippet children({ selected }: { selected: boolean })}
							<span>{o.label}</span>
							{#if selected}<Check size={13} />{/if}
						{/snippet}
					</Select.Item>
				{/each}
			</Select.Viewport>
		</Select.Content>
	</Select.Portal>
</Select.Root>
