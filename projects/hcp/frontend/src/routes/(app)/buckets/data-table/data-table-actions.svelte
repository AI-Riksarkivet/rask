<script lang="ts">
	import EllipsisIcon from 'lucide-svelte/icons/ellipsis';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';

	let {
		name,
		ondelete,
		onnavigate,
	}: {
		name: string;
		ondelete: () => void;
		onnavigate: () => void;
	} = $props();
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger>
		{#snippet child({ props })}
			<Button {...props} variant="ghost" size="icon" class="relative size-8 p-0">
				<span class="sr-only">Open menu</span>
				<EllipsisIcon class="size-4" />
			</Button>
		{/snippet}
	</DropdownMenu.Trigger>
	<DropdownMenu.Content align="end">
		<DropdownMenu.Group>
			<DropdownMenu.Label>Actions</DropdownMenu.Label>
			<DropdownMenu.Item onclick={() => navigator.clipboard.writeText(name)}>
				Copy bucket name
			</DropdownMenu.Item>
		</DropdownMenu.Group>
		<DropdownMenu.Separator />
		<DropdownMenu.Item onclick={onnavigate}>Open bucket</DropdownMenu.Item>
		<DropdownMenu.Item class="text-destructive" onclick={ondelete}>Delete bucket</DropdownMenu.Item>
	</DropdownMenu.Content>
</DropdownMenu.Root>
