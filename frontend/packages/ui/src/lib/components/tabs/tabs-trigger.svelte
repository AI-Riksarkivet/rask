<script lang="ts">
	import { Tabs as TabsPrimitive } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	// The active tab is carried by `data-[state=active]` — Bits UI owns that attribute, so the
	// selected state can never drift from the primitive's own idea of which tab is open. Colour comes
	// from tokens (`bg-card`, `text-foreground`), so both modes theme at once.
	let {
		ref = $bindable(null),
		class: className,
		...restProps
	}: TabsPrimitive.TriggerProps = $props();
</script>

<TabsPrimitive.Trigger
	bind:ref
	data-slot="tabs-trigger"
	class={cn(
		`focus-visible:border-ring focus-visible:ring-ring/50 data-[state=active]:bg-card
		 data-[state=active]:text-foreground inline-flex h-[calc(100%-1px)] flex-1 items-center
		 justify-center gap-1.5 rounded-md border border-transparent px-3 py-1 text-sm font-medium
		 whitespace-nowrap transition-[color,box-shadow] focus-visible:ring-[3px]
		 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50
		 data-[state=active]:shadow-sm [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0`,
		className,
	)}
	{...restProps}
/>
