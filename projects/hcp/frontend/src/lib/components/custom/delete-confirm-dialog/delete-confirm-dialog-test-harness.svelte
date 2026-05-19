<script lang="ts">
	import DeleteConfirmDialog from './delete-confirm-dialog.svelte';

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
	Open Delete Dialog
</button>

<div data-testid="confirm-count" class="mt-2 text-xs text-muted-foreground">
	Confirmed: {confirmCount}
</div>

<DeleteConfirmDialog
	bind:open
	bind:force
	name="production-data"
	itemType="namespace"
	{loading}
	showForceOption
	onconfirm={handleConfirm}
/>
