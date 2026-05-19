<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import {
		list_multipart_uploads,
		abort_multipart_upload,
		type MultipartUploadEntry,
	} from '$lib/remote/buckets.remote.js';
	import { formatDate } from '$lib/utils/format.js';
	import { RefreshCw, Trash2, Upload } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		bucket,
	}: {
		bucket: string;
	} = $props();

	let uploadsData = $derived(list_multipart_uploads({ bucket, prefix: undefined }));
	let uploads = $derived((uploadsData?.current?.uploads ?? []) as MultipartUploadEntry[]);

	// --- Abort confirmation ---
	let abortDialogOpen = $state(false);
	let abortTarget = $state<MultipartUploadEntry | null>(null);
	let aborting = $state(false);

	function requestAbort(upload: MultipartUploadEntry) {
		abortTarget = upload;
		abortDialogOpen = true;
	}

	async function handleConfirmAbort() {
		if (!abortTarget) return;
		aborting = true;
		try {
			await abort_multipart_upload({
				bucket,
				key: abortTarget.Key,
				upload_id: abortTarget.UploadId,
			}).updates(uploadsData);
			abortDialogOpen = false;
			toast.success(`Aborted upload for "${abortTarget.Key}"`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to abort upload'));
		} finally {
			aborting = false;
		}
	}

	function refresh() {
		uploadsData.refresh();
	}
</script>

<Card.Root>
	<Card.Header class="pb-3">
		<div class="flex items-center justify-between">
			<div class="flex items-center gap-2">
				<Upload class="h-4 w-4 text-muted-foreground" />
				<Card.Title class="text-base">In-Progress Uploads</Card.Title>
			</div>
			<Button variant="ghost" size="icon" class="h-8 w-8" onclick={refresh}>
				<RefreshCw class="h-4 w-4" />
			</Button>
		</div>
		<Card.Description>
			Multipart uploads that have been initiated but not yet completed or aborted.
		</Card.Description>
	</Card.Header>
	{#await uploadsData}
		<Card.Content>
			<TableSkeleton rows={3} columns={4} />
		</Card.Content>
	{:then}
		<Card.Content>
			{#if uploadsData.current?.error}
				<div
					class="rounded-lg border border-destructive/20 bg-destructive/5 p-4 text-sm text-destructive"
				>
					{uploadsData.current.error}
				</div>
			{:else if uploads.length === 0}
				<div class="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
					No in-progress uploads
				</div>
			{:else}
				<Table.Root>
					<Table.Header>
						<Table.Row>
							<Table.Head>Key</Table.Head>
							<Table.Head>Upload ID</Table.Head>
							<Table.Head>Initiated</Table.Head>
							<Table.Head class="w-[80px]">Actions</Table.Head>
						</Table.Row>
					</Table.Header>
					<Table.Body>
						{#each uploads as upload (upload.UploadId)}
							<Table.Row>
								<Table.Cell class="max-w-[200px] truncate font-medium">
									{upload.Key}
								</Table.Cell>
								<Table.Cell class="max-w-[160px] truncate font-mono text-xs text-muted-foreground">
									{upload.UploadId}
								</Table.Cell>
								<Table.Cell class="text-xs text-muted-foreground">
									{upload.Initiated ? formatDate(upload.Initiated) : '—'}
								</Table.Cell>
								<Table.Cell>
									<Button
										variant="ghost"
										size="icon"
										class="h-7 w-7 text-muted-foreground hover:text-destructive"
										onclick={() => requestAbort(upload)}
									>
										<Trash2 class="h-3.5 w-3.5" />
									</Button>
								</Table.Cell>
							</Table.Row>
						{/each}
					</Table.Body>
				</Table.Root>
			{/if}
		</Card.Content>
	{/await}
</Card.Root>

<DeleteConfirmDialog
	bind:open={abortDialogOpen}
	name={abortTarget?.Key ?? ''}
	itemType="upload"
	description={abortTarget
		? `Are you sure you want to abort the multipart upload for "${abortTarget.Key}"? This will discard all uploaded parts.`
		: ''}
	loading={aborting}
	onconfirm={handleConfirmAbort}
/>
