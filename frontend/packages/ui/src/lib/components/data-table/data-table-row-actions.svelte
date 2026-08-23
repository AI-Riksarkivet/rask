<script lang="ts">
	import type { Snippet } from 'svelte';
	import { Ellipsis } from '@lucide/svelte';
	import { Button } from '../button/index.js';
	import * as DropdownMenu from '../dropdown-menu/index.js';

	// The per-row actions slot: an ellipsis trigger opening a dropdown whose items the consumer
	// supplies as `children` (DropdownMenu.Item/Separator/Label from @rask/ui). Wire it from a
	// ColumnDef cell via renderSnippet — snippets can close over the row, components can't:
	// `cell: ({ row }) => renderSnippet(actions, row.original)` with
	// `{#snippet actions(row)}<DataTableRowActions>…</DataTableRowActions>{/snippet}`.
	let { label = 'Open menu', children }: { label?: string; children: Snippet } = $props();
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger>
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				size="icon-sm"
				onclick={(e: MouseEvent) => {
					// Keep the trigger from also firing the table's row-level onrowclick.
					e.stopPropagation();
					(props as { onclick?: (e: MouseEvent) => void }).onclick?.(e);
				}}
			>
				<span class="sr-only">{label}</span>
				<Ellipsis class="size-4" />
			</Button>
		{/snippet}
	</DropdownMenu.Trigger>
	<DropdownMenu.Content align="end">
		{@render children()}
	</DropdownMenu.Content>
</DropdownMenu.Root>
