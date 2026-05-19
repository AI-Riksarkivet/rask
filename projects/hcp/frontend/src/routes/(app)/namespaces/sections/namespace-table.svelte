<script lang="ts">
	import { goto } from '$app/navigation';
	import { Search, Trash2, Shield } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { toast } from 'svelte-sonner';
	import { useDelete } from '$lib/utils/use-delete.svelte.js';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import BulkDeleteDialog from '$lib/components/custom/bulk-delete-dialog/bulk-delete-dialog.svelte';
	import StorageProgressBar from '$lib/components/custom/storage-progress-bar/storage-progress-bar.svelte';
	import ServiceTagBadge from '$lib/components/custom/service-tag-badge/service-tag-badge.svelte';
	import type { RemoteQuery } from '@sveltejs/kit';
	import {
		update_namespace,
		delete_namespace,
		type Namespace,
	} from '$lib/remote/namespaces.remote.js';
	import { type ChargebackReport } from '$lib/remote/tenant-info.remote.js';
	import {
		formatBytes,
		parseQuotaBytes,
		buildStorageMap,
		type ChargebackEntry,
	} from '$lib/utils/format.js';
	import {
		DataTable,
		DataTableCheckbox,
		DataTableHeaderButton,
		createSvelteTable,
		getCoreRowModel,
		getSortedRowModel,
		getPaginationRowModel,
		renderSnippet,
		renderComponent,
	} from '$lib/components/ui/data-table/index.js';
	import type { ColumnDef, SortingState, PaginationState } from '@tanstack/table-core';
	import DataTableActions from '../data-table/data-table-actions.svelte';
	import DataTableTagCell from '../data-table/data-table-tag-cell.svelte';

	let {
		tenant,
		nsData,
		chargebackData,
		ongrantaccess,
	}: {
		tenant: string;
		nsData: RemoteQuery<Namespace[]>;
		chargebackData: RemoteQuery<ChargebackReport>;
		ongrantaccess?: (namespaceNames: string[]) => void;
	} = $props();

	let chargeback = $derived((chargebackData?.current?.chargebackData ?? []) as ChargebackEntry[]);
	let nsStorageMap = $derived(buildStorageMap(chargeback));

	let search = $state('');
	let namespaces = $derived((nsData?.current ?? []) as Namespace[]);
	let filteredNamespaces = $derived(
		namespaces.filter((n) => n.name.toLowerCase().includes(search.toLowerCase()))
	);

	// TanStack Table state
	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });
	let rowSelection = $state<Record<string, boolean>>({});

	let selectedKeys = $derived(
		Object.keys(rowSelection)
			.filter((k) => rowSelection[k])
			.map((k) => nsTable.getCoreRowModel().rows[Number(k)]?.original.name)
			.filter(Boolean) as string[]
	);
	let selectedCount = $derived(selectedKeys.length);

	const del = useDelete({ entityName: 'namespace' });

	function onConfirmDelete() {
		del.confirmDelete(() => delete_namespace({ tenant, name: del.deleteTarget }).updates(nsData!));
	}

	function onConfirmBulkDelete() {
		del.confirmBulkDelete(
			selectedKeys,
			(name, isLast) => {
				const call = delete_namespace({ tenant, name });
				return isLast ? call.updates(nsData!) : call;
			},
			() => (rowSelection = {})
		);
	}

	// Tag editing for existing namespaces
	let editingTagsNs = $state('');

	function startEditTags(ns: Namespace) {
		editingTagsNs = ns.name;
	}

	async function handleSaveTags(nsName: string, tags: string[]) {
		if (!nsData) return;
		try {
			await update_namespace({
				tenant,
				name: nsName,
				body: { tags: { tag: tags } },
			}).updates(nsData);
			toast.success('Tags updated');
			editingTagsNs = '';
		} catch {
			toast.error('Failed to update tags');
		}
	}

	// TanStack Table columns
	let nsColumns = $derived.by((): ColumnDef<Namespace>[] => [
		{
			id: 'select',
			header: ({ table }) =>
				renderComponent(DataTableCheckbox, {
					checked: table.getIsAllPageRowsSelected(),
					onCheckedChange: (val: boolean) => table.toggleAllPageRowsSelected(!!val),
					'aria-label': 'Select all rows',
				}),
			cell: ({ row }) =>
				renderComponent(DataTableCheckbox, {
					checked: row.getIsSelected(),
					onCheckedChange: (val: boolean) => row.toggleSelected(!!val),
					'aria-label': `Select ${row.original.name}`,
				}),
			meta: { headerClass: 'w-10 px-4 py-3', cellClass: 'px-4 py-3' },
		},
		{
			accessorKey: 'name',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Name',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nameCellSnippet, row.original),
			meta: { cellClass: 'px-4 py-3 font-medium' },
		},
		{
			id: 'storage',
			header: 'Storage Used',
			cell: ({ row }) => renderSnippet(storageCellSnippet, row.original),
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'tags',
			header: 'Tags',
			cell: ({ row }) =>
				renderComponent(DataTableTagCell, {
					tags: row.original.tags?.tag ?? [],
					editing: editingTagsNs === row.original.name,
					onsave: (tags: string[]) => handleSaveTags(row.original.name, tags),
					onstartedit: () => startEditTags(row.original),
					oncanceledit: () => (editingTagsNs = ''),
				}),
		},
		{
			accessorKey: 'description',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Description',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => (row.original.description ?? '—') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			accessorKey: 'hardQuota',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Hard Quota',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => (row.original.hardQuota ?? '—') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'softQuota',
			header: 'Soft Quota',
			cell: ({ row }) =>
				(row.original.softQuota != null ? `${row.original.softQuota}%` : '—') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'hashScheme',
			header: 'Hash Scheme',
			cell: ({ row }) => renderSnippet(hashSchemeCellSnippet, row.original),
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) =>
				renderComponent(DataTableActions, {
					name: row.original.name,
					ondelete: () => del.requestDelete(row.original.name),
					onnavigate: () => goto(`/namespaces/${row.original.name}`),
					onedittags: () => startEditTags(row.original),
				}),
			meta: { headerClass: 'w-16 px-4 py-3', cellClass: 'px-4 py-3' },
		},
	]);

	let nsTable = $derived(
		createSvelteTable({
			get data() {
				return filteredNamespaces;
			},
			get columns() {
				return nsColumns;
			},
			state: {
				get sorting() {
					return sorting;
				},
				get pagination() {
					return pagination;
				},
				get rowSelection() {
					return rowSelection;
				},
			},
			onSortingChange: (updater) => {
				sorting = typeof updater === 'function' ? updater(sorting) : updater;
			},
			onPaginationChange: (updater) => {
				pagination = typeof updater === 'function' ? updater(pagination) : updater;
			},
			onRowSelectionChange: (updater) => {
				rowSelection = typeof updater === 'function' ? updater(rowSelection) : updater;
			},
			getCoreRowModel: getCoreRowModel(),
			getSortedRowModel: getSortedRowModel(),
			getPaginationRowModel: getPaginationRowModel(),
			enableRowSelection: true,
		})
	);
</script>

{#snippet nameCellSnippet(ns: Namespace)}
	<a
		href="/namespaces/{ns.name}"
		class="text-primary underline-offset-4 hover:underline"
		onclick={(e: MouseEvent) => e.stopPropagation()}
	>
		{ns.name}
	</a>
{/snippet}

{#snippet storageCellSnippet(ns: Namespace)}
	{@const used = nsStorageMap.get(ns.name) ?? 0}
	{@const quota = ns.hardQuota ? parseQuotaBytes(ns.hardQuota) : null}
	{#if used > 0 || quota}
		<div class="flex flex-col gap-1">
			<span class="text-sm">{formatBytes(used)}{quota ? ` / ${ns.hardQuota}` : ''}</span>
			{#if quota}
				{@const pct = (used / quota) * 100}
				<StorageProgressBar percent={pct} class="max-w-24" />
			{/if}
		</div>
	{:else}
		—
	{/if}
{/snippet}

{#snippet hashSchemeCellSnippet(ns: Namespace)}
	{#if ns.hashScheme}
		<Badge variant="secondary">{ns.hashScheme}</Badge>
	{:else}
		<span class="text-muted-foreground">—</span>
	{/if}
{/snippet}

{#await nsData}
	<TableSkeleton rows={5} columns={5} />
{:then}
	<div class="space-y-2">
		<div class="relative max-w-md">
			<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
			<Input bind:value={search} placeholder="Search namespaces..." class="pl-10" />
		</div>
	</div>

	{#if selectedCount > 0}
		<div class="flex items-center gap-3 rounded-lg border bg-muted/50 px-4 py-2">
			<span class="text-sm font-medium">{selectedCount} selected</span>
			<Button variant="destructive" size="sm" onclick={() => del.requestBulkDelete()}>
				<Trash2 class="h-3.5 w-3.5" />Delete Selected
			</Button>
			<Button variant="outline" size="sm" onclick={() => ongrantaccess?.(selectedKeys)}>
				<Shield class="h-3.5 w-3.5" />Grant Access
			</Button>
			<Button variant="ghost" size="sm" onclick={() => (rowSelection = {})}>Deselect All</Button>
		</div>
	{/if}

	<DataTable
		table={nsTable}
		noResultsMessage={namespaces.length === 0
			? 'No namespaces found. Create one to get started.'
			: `No results matching "${search}"`}
	>
		{#snippet footer()}
			{#if selectedCount > 0}
				{selectedCount} of {filteredNamespaces.length} row(s) selected.
			{/if}
		{/snippet}
	</DataTable>
{/await}

<DeleteConfirmDialog
	bind:open={del.deleteDialogOpen}
	name={del.deleteTarget}
	itemType="namespace"
	loading={del.deleting}
	onconfirm={onConfirmDelete}
/>

<BulkDeleteDialog
	bind:open={del.bulkDeleteOpen}
	count={selectedCount}
	itemType="namespace"
	loading={del.deleting}
	onconfirm={onConfirmBulkDelete}
/>
