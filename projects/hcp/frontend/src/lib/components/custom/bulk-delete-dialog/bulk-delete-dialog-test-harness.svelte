<script lang="ts">
	import BulkDeleteDialog from './bulk-delete-dialog.svelte';

	let open = $state(true);
	let force = $state(false);
	let loading = $state(false);
	let confirmCount = $state(0);

	function handleConfirm() {
		confirmCount++;
		loading = true;
		setTimeout(() => {
			loading = false;
			open = false;
		}, 500);
	}
</script>

<button onclick={() => (open = true)} class="rounded bg-destructive px-4 py-2 text-sm text-white">
	Open Bulk Delete Dialog
</button>

<div data-testid="confirm-count" class="mt-2 text-xs text-muted-foreground">
	Confirmed: {confirmCount}
</div>

<BulkDeleteDialog
	bind:open
	bind:force
	count={5}
	itemType="namespace"
	{loading}
	showForceOption
	onconfirm={handleConfirm}
/>
