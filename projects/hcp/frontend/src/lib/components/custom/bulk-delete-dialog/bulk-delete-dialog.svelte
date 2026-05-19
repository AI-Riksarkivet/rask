<script lang="ts">
	import * as AlertDialog from '$lib/components/ui/alert-dialog/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';

	let {
		open = $bindable(false),
		force = $bindable(false),
		count,
		itemType,
		loading = false,
		showForceOption = false,
		onconfirm,
	}: {
		open: boolean;
		force?: boolean;
		count: number;
		itemType: string;
		loading?: boolean;
		showForceOption?: boolean;
		onconfirm: () => void;
	} = $props();

	let plural = $derived(count !== 1 ? 's' : '');

	// Reset force when dialog closes
	$effect(() => {
		if (!open) force = false;
	});
</script>

<AlertDialog.Root bind:open>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>Delete {count} {itemType}{plural}</AlertDialog.Title>
			<AlertDialog.Description>
				Are you sure you want to delete {count}
				{itemType}{plural}? This action cannot be undone.
			</AlertDialog.Description>
			{#if showForceOption}
				<label class="mt-3 flex items-center gap-2">
					<Checkbox bind:checked={force} disabled={loading} />
					<span class="text-sm text-muted-foreground">
						Force delete — empties bucket and overrides versioning protection settings
					</span>
				</label>
			{/if}
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel disabled={loading}>Cancel</AlertDialog.Cancel>
			<Button variant="destructive" onclick={onconfirm} disabled={loading}>
				{loading ? 'Deleting...' : `Delete ${count} ${itemType}${plural}`}
			</Button>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>
