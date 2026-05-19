<script lang="ts">
	import { Plus } from 'lucide-svelte';
	import EllipsisIcon from 'lucide-svelte/icons/ellipsis';
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import {
		DataTable,
		createSvelteTable,
		getCoreRowModel,
		renderSnippet,
	} from '$lib/components/ui/data-table/index.js';
	import type { ColumnDef } from '@tanstack/table-core';
	import { toast } from 'svelte-sonner';
	import {
		get_retention_classes,
		create_retention_class,
		update_retention_class,
		delete_retention_class,
		type RetentionClass,
	} from '$lib/remote/namespaces.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let rcData = $derived(get_retention_classes({ tenant, name: namespaceName }));
	let retentionClasses = $derived((rcData?.current ?? []) as RetentionClass[]);

	// --- Create dialog ---
	let createOpen = $state(false);
	let createError = $state('');
	let creating = $state(false);

	async function handleCreate(e: SubmitEvent) {
		e.preventDefault();
		if (!rcData) return;
		const form = e.currentTarget as HTMLFormElement;
		const fd = new FormData(form);
		const name = (fd.get('name') as string).trim();
		const value = (fd.get('value') as string).trim();
		const description = (fd.get('description') as string)?.trim() || undefined;
		const allowDisposition = fd.has('allowDisposition');
		if (!name || !value) return;

		creating = true;
		createError = '';
		try {
			await create_retention_class({
				tenant,
				namespace: namespaceName,
				body: { name, value, description, allowDisposition },
			}).updates(rcData);
			toast.success(`Retention class "${name}" created`);
			createOpen = false;
			form.reset();
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create retention class');
		} finally {
			creating = false;
		}
	}

	// --- Edit dialog ---
	let editOpen = $state(false);
	let editError = $state('');
	let editing = $state(false);
	let editTarget = $state<RetentionClass | null>(null);

	function openEdit(rc: RetentionClass) {
		editTarget = rc;
		editError = '';
		editOpen = true;
	}

	async function handleEdit(e: SubmitEvent) {
		e.preventDefault();
		if (!rcData || !editTarget?.name) return;
		const form = e.currentTarget as HTMLFormElement;
		const fd = new FormData(form);
		const value = (fd.get('value') as string)?.trim() || undefined;
		const description = (fd.get('description') as string)?.trim() || undefined;
		const allowDisposition = fd.has('allowDisposition');

		editing = true;
		editError = '';
		try {
			await update_retention_class({
				tenant,
				namespace: namespaceName,
				className: editTarget.name,
				body: { value, description, allowDisposition },
			}).updates(rcData);
			toast.success(`Retention class "${editTarget.name}" updated`);
			editOpen = false;
		} catch (err) {
			editError = getErrorMessage(err, 'Failed to update retention class');
		} finally {
			editing = false;
		}
	}

	// --- Delete ---
	let deleteOpen = $state(false);
	let deleteTarget = $state('');
	let deleting = $state(false);

	function requestDelete(name: string) {
		deleteTarget = name;
		deleteOpen = true;
	}

	async function handleDelete() {
		if (!rcData || !deleteTarget) return;
		deleting = true;
		try {
			await delete_retention_class({
				tenant,
				namespace: namespaceName,
				className: deleteTarget,
			}).updates(rcData);
			toast.success(`Retention class "${deleteTarget}" deleted`);
			deleteOpen = false;
		} catch {
			toast.error('Failed to delete retention class');
		} finally {
			deleting = false;
		}
	}

	// --- Table ---
	let columns = $derived.by((): ColumnDef<RetentionClass>[] => [
		{
			accessorKey: 'name',
			header: 'Name',
			meta: { cellClass: 'px-4 py-2 font-medium' },
		},
		{
			accessorKey: 'value',
			header: 'Value',
			meta: { cellClass: 'px-4 py-2 text-muted-foreground' },
		},
		{
			id: 'disposition',
			header: 'Disp.',
			cell: ({ row }) => renderSnippet(dispositionCell, row.original),
			meta: { cellClass: 'px-4 py-2' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { headerClass: 'w-12', cellClass: 'px-2 py-2' },
		},
	]);

	let table = $derived(
		createSvelteTable({
			get data() {
				return retentionClasses;
			},
			get columns() {
				return columns;
			},
			getCoreRowModel: getCoreRowModel(),
		})
	);
</script>

{#snippet dispositionCell(rc: RetentionClass)}
	<Badge variant={rc.allowDisposition ? 'default' : 'outline'} class="text-xs">
		{rc.allowDisposition ? 'Yes' : 'No'}
	</Badge>
{/snippet}

{#snippet actionsCell(rc: RetentionClass)}
	<DropdownMenu.Root>
		<DropdownMenu.Trigger>
			{#snippet child({ props })}
				<Button {...props} variant="ghost" size="icon" class="relative size-7 p-0">
					<span class="sr-only">Open menu</span>
					<EllipsisIcon class="size-4" />
				</Button>
			{/snippet}
		</DropdownMenu.Trigger>
		<DropdownMenu.Content align="end">
			<DropdownMenu.Item onclick={() => openEdit(rc)}>Edit</DropdownMenu.Item>
			<DropdownMenu.Item class="text-destructive" onclick={() => requestDelete(rc.name ?? '')}>
				Delete
			</DropdownMenu.Item>
		</DropdownMenu.Content>
	</DropdownMenu.Root>
{/snippet}

<Card.Root class="flex h-full flex-col">
	<Card.Header>
		<Card.Title>Retention Classes</Card.Title>
		<Card.Description>
			Predefined retention policies for standardized object retention.
		</Card.Description>
		<Card.Action>
			<Button size="sm" onclick={() => (createOpen = true)}>
				<Plus class="h-4 w-4" />
				Create
			</Button>
		</Card.Action>
	</Card.Header>
	{#await rcData}
		<Card.Content class="flex-1">
			<TableSkeleton rows={3} columns={4} />
		</Card.Content>
	{:then}
		<Card.Content class="flex-1">
			{#if retentionClasses.length === 0}
				<div class="rounded-lg border border-dashed p-6 text-center">
					<p class="text-sm text-muted-foreground">
						No retention classes. Create one to enforce retention policies.
					</p>
				</div>
			{:else}
				<DataTable {table} noResultsMessage="No retention classes found." />
			{/if}
		</Card.Content>
	{/await}
</Card.Root>

<!-- Create Dialog -->
<Dialog.Root bind:open={createOpen}>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Header>
			<Dialog.Title>Create Retention Class</Dialog.Title>
		</Dialog.Header>
		<form onsubmit={handleCreate} class="space-y-4">
			<ErrorBanner message={createError} />
			<div class="space-y-2">
				<Label for="rc-name">Name</Label>
				<Input id="rc-name" name="name" placeholder="e.g. 7-year-hold" required />
			</div>
			<div class="space-y-2">
				<Label for="rc-value">Value</Label>
				<Input id="rc-value" name="value" placeholder="e.g. A+7y or -1" required />
				<p class="text-xs text-muted-foreground">
					Retention duration using offsets (e.g. A+7y for 7 years from creation) or -1 for
					indefinite.
				</p>
			</div>
			<div class="space-y-2">
				<Label for="rc-desc">Description</Label>
				<Input id="rc-desc" name="description" placeholder="Optional description" />
			</div>
			<div class="space-y-1">
				<div class="flex items-center gap-3">
					<Checkbox id="rc-disposition" name="allowDisposition" />
					<Label for="rc-disposition">Allow disposition</Label>
				</div>
				<p class="text-xs text-muted-foreground pl-7">
					Allow automatic deletion of objects when their retention period expires.
				</p>
			</div>
			<Dialog.Footer>
				<Button variant="ghost" type="button" onclick={() => (createOpen = false)}>Cancel</Button>
				<Button type="submit" disabled={creating}>
					{creating ? 'Creating...' : 'Create'}
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>

<!-- Edit Dialog -->
<Dialog.Root bind:open={editOpen}>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Header>
			<Dialog.Title>Edit Retention Class: {editTarget?.name}</Dialog.Title>
		</Dialog.Header>
		<form onsubmit={handleEdit} class="space-y-4">
			<ErrorBanner message={editError} />
			<div class="space-y-2">
				<Label for="edit-rc-value">Value</Label>
				<Input
					id="edit-rc-value"
					name="value"
					placeholder="e.g. A+7y or -1"
					value={editTarget?.value ?? ''}
				/>
			</div>
			<div class="space-y-2">
				<Label for="edit-rc-desc">Description</Label>
				<Input id="edit-rc-desc" name="description" value={editTarget?.description ?? ''} />
			</div>
			<div class="space-y-1">
				<div class="flex items-center gap-3">
					<Checkbox
						id="edit-rc-disposition"
						name="allowDisposition"
						checked={editTarget?.allowDisposition ?? false}
					/>
					<Label for="edit-rc-disposition">Allow disposition</Label>
				</div>
				<p class="text-xs text-muted-foreground pl-7">
					Allow automatic deletion of objects when their retention period expires.
				</p>
			</div>
			<Dialog.Footer>
				<Button variant="ghost" type="button" onclick={() => (editOpen = false)}>Cancel</Button>
				<Button type="submit" disabled={editing}>
					{editing ? 'Saving...' : 'Save'}
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>

<!-- Delete Confirm -->
<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={deleteTarget}
	itemType="retention class"
	loading={deleting}
	onconfirm={handleDelete}
/>
