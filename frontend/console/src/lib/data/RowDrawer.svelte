<script lang="ts">
	// A right-side row drawer (goal cond 8) on the bits-ui Dialog primitive (via @rask/ui/dialog),
	// styled as a sheet: registry rows open it with the full record + metadata + jump links. The
	// content snippet is authored by the owning registry, so its scoped styles apply; closing is
	// Esc / overlay / the explicit button (bits-ui handles focus trap + restore).
	import { Dialog } from '@rask/ui/dialog';
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

<Dialog.Root bind:open>
	<Dialog.Content
		class="top-0 right-0 bottom-0 left-auto flex h-full max-h-full w-[400px] max-w-full translate-x-0 translate-y-0 flex-col items-stretch gap-4 overflow-y-auto rounded-none"
	>
		<Dialog.Title class="font-mono text-base break-all">{title}</Dialog.Title>
		<Dialog.Description>{description}</Dialog.Description>
		{@render children()}
		<div class="mt-auto flex justify-end">
			<button class="close" onclick={() => (open = false)}>Close</button>
		</div>
	</Dialog.Content>
</Dialog.Root>

<style>
	.close {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.close:hover {
		border-color: var(--mut);
	}
</style>
