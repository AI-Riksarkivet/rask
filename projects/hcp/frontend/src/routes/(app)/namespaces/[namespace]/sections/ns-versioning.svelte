<script lang="ts">
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import * as AlertDialog from '$lib/components/ui/alert-dialog/index.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { toast } from 'svelte-sonner';
	import {
		get_ns_versioning,
		update_ns_versioning,
		delete_ns_versioning,
		type VersioningSettings,
	} from '$lib/remote/namespaces.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let versioningData = $derived(get_ns_versioning({ tenant, name: namespaceName }));
	let versioning = $derived((versioningData?.current ?? {}) as VersioningSettings);

	const saver = useSave({
		successMsg: 'Versioning settings updated',
		errorMsg: 'Failed to update versioning settings',
	});

	let localKeepDeletionRecords = $state(false);
	let localUseDeleteMarkers = $state(false);
	let localPrune = $state(false);
	let localPruneDays = $state(0);

	$effect(() => {
		const v = versioning;
		void saver.syncVersion;
		localKeepDeletionRecords = v.keepDeletionRecords ?? false;
		localUseDeleteMarkers = v.useDeleteMarkers ?? false;
		localPrune = v.prune ?? false;
		localPruneDays = v.pruneDays ?? 0;
	});

	let dirty = $derived(
		localKeepDeletionRecords !== (versioning.keepDeletionRecords ?? false) ||
			localUseDeleteMarkers !== (versioning.useDeleteMarkers ?? false) ||
			localPrune !== (versioning.prune ?? false) ||
			localPruneDays !== (versioning.pruneDays ?? 0)
	);

	let resetOpen = $state(false);
	let resetting = $state(false);

	async function handleReset() {
		resetting = true;
		try {
			if (!versioningData) return;
			await delete_ns_versioning({ tenant, name: namespaceName }).updates(versioningData);
			toast.success('Versioning settings reset to defaults');
			resetOpen = false;
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to reset versioning settings'));
		} finally {
			resetting = false;
		}
	}
</script>

<Card.Root class="flex h-full flex-col">
	<Card.Header>
		<Card.Title>Versioning</Card.Title>
		<Card.Description>
			Controls how object versions, delete markers, and deletion records are managed.
		</Card.Description>
	</Card.Header>
	{#await versioningData}
		<Card.Content class="flex-1">
			<div class="flex flex-wrap gap-6">
				{#each Array(4) as _, i (i)}
					<div class="h-5 w-32 animate-pulse rounded bg-muted"></div>
				{/each}
			</div>
		</Card.Content>
	{:then}
		<Card.Content class="flex-1 space-y-4">
			<div class="flex flex-wrap gap-x-6 gap-y-3">
				<div class="space-y-1.5">
					<div class="flex items-center gap-2">
						<Switch id="keep-deletion-records" bind:checked={localKeepDeletionRecords} />
						<Label for="keep-deletion-records" class="text-sm">Keep Deletion Records</Label>
					</div>
					<p class="text-xs text-muted-foreground">
						Retain records of delete operations for compliance auditing. Prevents bucket deletion
						when records exist.
					</p>
				</div>
				<div class="space-y-1.5">
					<div class="flex items-center gap-2">
						<Switch id="use-delete-markers" bind:checked={localUseDeleteMarkers} />
						<Label for="use-delete-markers" class="text-sm">Use Delete Markers</Label>
					</div>
					<p class="text-xs text-muted-foreground">
						Create delete markers when objects are deleted instead of permanently removing them.
						Irreversible once enabled.
					</p>
				</div>
			</div>
			<div class="space-y-1.5">
				<div class="flex items-center gap-2">
					<Switch id="prune" bind:checked={localPrune} />
					<Label for="prune" class="text-sm">Version Pruning</Label>
				</div>
				<p class="text-xs text-muted-foreground">
					Automatically remove old object versions after the retention period.
				</p>
			</div>
			<div class="space-y-1.5">
				<Label for="prune-days">Prune After (days)</Label>
				<Input
					id="prune-days"
					type="number"
					min={0}
					disabled={!localPrune}
					bind:value={localPruneDays}
				/>
				<p class="text-xs text-muted-foreground">
					Days to keep old versions before pruning. 0 = prune immediately.
				</p>
			</div>
		</Card.Content>
		<Card.Footer class="flex justify-between">
			<Button variant="outline" size="sm" onclick={() => (resetOpen = true)}>
				Reset to Defaults
			</Button>
			<SaveButton
				{dirty}
				saving={saver.saving}
				onclick={() =>
					saver.run(async () => {
						if (!versioningData) return;
						await update_ns_versioning({
							tenant,
							name: namespaceName,
							body: {
								keepDeletionRecords: localKeepDeletionRecords,
								useDeleteMarkers: localUseDeleteMarkers,
								prune: localPrune,
								pruneDays: localPruneDays,
							},
						}).updates(versioningData);
					})}
			/>
		</Card.Footer>
	{/await}
</Card.Root>

<AlertDialog.Root bind:open={resetOpen}>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>Reset Versioning Settings</AlertDialog.Title>
			<AlertDialog.Description>
				This will remove all versioning configuration for namespace "<strong>{namespaceName}</strong
				>" and revert to no-versioning state. All settings (pruning, delete markers, deletion
				records) will be cleared.
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel disabled={resetting}>Cancel</AlertDialog.Cancel>
			<Button variant="destructive" onclick={handleReset} disabled={resetting}>
				{resetting ? 'Resetting...' : 'Reset'}
			</Button>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>
