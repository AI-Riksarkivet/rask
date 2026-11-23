<script lang="ts">
	// A right-side row drawer (goal cond 8), the design system's sheet: registry rows open it with the
	// full record + metadata + jump links. The content snippet is authored by the owning registry, so
	// its scoped styles apply; closing is Esc / overlay / the explicit button (bits-ui handles focus
	// trap + restore). The sheet's own corner close is off, so the drawer has one control named Close.
	import { buttonVariants } from '@riksarkivet/ui/button';
	import * as Sheet from '@riksarkivet/ui/sheet';
	import type { Snippet } from 'svelte';

	let {
		open = $bindable(false),
		title,
		description,
		children,
	}: {
		open?: boolean;
		title: string;
		description: string;
		children: Snippet;
	} = $props();
</script>

<Sheet.Root bind:open>
	<Sheet.Content side="right" showCloseButton={false}>
		<Sheet.Header>
			<Sheet.Title class="font-mono text-base break-all">{title}</Sheet.Title>
			<Sheet.Description>{description}</Sheet.Description>
		</Sheet.Header>
		<div class="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4">
			{@render children()}
		</div>
		<Sheet.Footer class="flex-row justify-end">
			<Sheet.Close class={buttonVariants({ variant: 'outline', size: 'sm' })}>Close</Sheet.Close>
		</Sheet.Footer>
	</Sheet.Content>
</Sheet.Root>
