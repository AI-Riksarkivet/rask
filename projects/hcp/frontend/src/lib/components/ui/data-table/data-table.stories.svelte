<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import DataTable from './data-table.svelte';

	interface Namespace {
		name: string;
		description: string;
		hardQuota: string;
		softQuota: number;
		storageUsed: number;
		hashScheme: string;
		tags: { tag: string[] };
	}

	const { Story } = defineMeta({
		title: 'UI/DataTable',
		component: DataTable,
	});
</script>

<script lang="ts">
	import { Search, Trash2, Shield } from 'lucide-svelte';
	import {
		createSvelteTable,
		getCoreRowModel,
		getSortedRowModel,
		getPaginationRowModel,
		getFilteredRowModel,
		renderComponent,
		renderSnippet,
	} from './index.js';
	import type { ColumnDef, SortingState } from '@tanstack/table-core';
	import DataTableHeaderButton from './data-table-header-button.svelte';
	import DataTableCheckbox from './data-table-checkbox.svelte';
	import DataTableActions from '../../../../routes/(app)/namespaces/data-table/data-table-actions.svelte';
	import DataTableTagCell from '../../../../routes/(app)/namespaces/data-table/data-table-tag-cell.svelte';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import StorageProgressBar from '$lib/components/custom/storage-progress-bar/storage-progress-bar.svelte';
	import { toast } from 'svelte-sonner';

	interface Namespace {
		name: string;
		description: string;
		hardQuota: string;
		softQuota: number;
		storageUsed: number;
		hashScheme: string;
		tags: { tag: string[] };
	}

	// ─── Mock data ───────────────────────────────────────────────────
	const NAMESPACES = [
		{
			name: 'production-data',
			description: 'Main production namespace',
			hardQuota: '500 GB',
			softQuota: 85,
			storageUsed: 342_000_000_000,
			hashScheme: 'SHA-256',
			tags: { tag: ['production', 'critical', 's3'] },
		},
		{
			name: 'staging-env',
			description: 'Staging environment',
			hardQuota: '100 GB',
			softQuota: 80,
			storageUsed: 45_000_000_000,
			hashScheme: 'SHA-256',
			tags: { tag: ['staging'] },
		},
		{
			name: 'dev-sandbox',
			description: 'Developer sandbox',
			hardQuota: '50 GB',
			softQuota: 90,
			storageUsed: 48_000_000_000,
			hashScheme: 'MD5',
			tags: { tag: ['development'] },
		},
		{
			name: 'analytics-warehouse',
			description: 'Analytics data lake',
			hardQuota: '1 TB',
			softQuota: 80,
			storageUsed: 780_000_000_000,
			hashScheme: 'SHA-256',
			tags: { tag: ['analytics', 'hdfs'] },
		},
		{
			name: 'backup-vault',
			description: 'Encrypted backups',
			hardQuota: '2 TB',
			softQuota: 95,
			storageUsed: 1_200_000_000_000,
			hashScheme: 'SHA-512',
			tags: { tag: ['backup', 'nfs'] },
		},
		{
			name: 'media-assets',
			description: 'Images and videos',
			hardQuota: '200 GB',
			softQuota: 75,
			storageUsed: 67_000_000_000,
			hashScheme: 'SHA-256',
			tags: { tag: ['media', 'cifs'] },
		},
		{
			name: 'compliance-archive',
			description: 'Regulatory compliance data',
			hardQuota: '500 GB',
			softQuota: 90,
			storageUsed: 490_000_000_000,
			hashScheme: 'SHA-256',
			tags: { tag: ['compliance', 'lakefs'] },
		},
		{
			name: 'ml-training',
			description: 'Machine learning datasets',
			hardQuota: '1 TB',
			softQuota: 85,
			storageUsed: 234_000_000_000,
			hashScheme: 'SHA-256',
			tags: { tag: ['ml', 's3'] },
		},
		{
			name: 'logs-archive',
			description: 'Centralized log storage',
			hardQuota: '300 GB',
			softQuota: 70,
			storageUsed: 289_000_000_000,
			hashScheme: 'MD5',
			tags: { tag: ['logs', 'smtp'] },
		},
		{
			name: 'shared-assets',
			description: 'Cross-team shared files',
			hardQuota: '100 GB',
			softQuota: 80,
			storageUsed: 12_000_000_000,
			hashScheme: 'SHA-256',
			tags: { tag: [] },
		},
	];

	function formatBytes(bytes: number) {
		if (bytes === 0) return '0 B';
		const k = 1024;
		const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
		const i = Math.floor(Math.log(bytes) / Math.log(k));
		return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
	}

	function parseQuota(quota: string) {
		const match = quota.match(/^([\d.]+)\s*(GB|TB)$/i);
		if (!match) return 0;
		const val = parseFloat(match[1]);
		return match[2].toUpperCase() === 'TB'
			? val * 1024 * 1024 * 1024 * 1024
			: val * 1024 * 1024 * 1024;
	}

	// ─── Full table (search + select + actions + tags) ───────────────
	let search = $state('');
	let filteredData = $derived(
		NAMESPACES.filter((n) => n.name.toLowerCase().includes(search.toLowerCase()))
	);
	let sorting: SortingState = $state([]);
	let pagination = $state({ pageIndex: 0, pageSize: 25 });
	let rowSelection: Record<string, boolean> = $state({});
	let editingTagsNs = $state('');

	let selectedKeys = $derived(
		Object.keys(rowSelection)
			.filter((k) => rowSelection[k])
			.map((k) => fullTable.getCoreRowModel().rows[Number(k)]?.original.name)
			.filter(Boolean)
	);
	let selectedCount = $derived(selectedKeys.length);

	function handleSaveTags(nsName: string, tags: string[]) {
		toast.success(`Tags saved for ${nsName}: ${tags.join(', ') || '(none)'}`);
		editingTagsNs = '';
	}

	const fullColumns: ColumnDef<Namespace, unknown>[] = [
		{
			id: 'select',
			header: ({ table }) =>
				renderComponent(DataTableCheckbox, {
					checked: table.getIsAllPageRowsSelected(),
					onCheckedChange: (val) => table.toggleAllPageRowsSelected(!!val),
					'aria-label': 'Select all rows',
				}),
			cell: ({ row }) =>
				renderComponent(DataTableCheckbox, {
					checked: row.getIsSelected(),
					onCheckedChange: (val) => row.toggleSelected(!!val),
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
					onsave: (tags) => handleSaveTags(row.original.name, tags),
					onstartedit: () => (editingTagsNs = row.original.name),
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
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			accessorKey: 'hardQuota',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Hard Quota',
					onclick: column.getToggleSortingHandler(),
				}),
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'softQuota',
			header: 'Soft Quota',
			cell: ({ row }) => (row.original.softQuota != null ? `${row.original.softQuota}%` : '—'),
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
					ondelete: () => toast.error(`Delete: ${row.original.name}`),
					onnavigate: () => toast.info(`Navigate to: ${row.original.name}`),
					onedittags: () => (editingTagsNs = row.original.name),
				}),
			meta: { headerClass: 'w-16 px-4 py-3', cellClass: 'px-4 py-3' },
		},
	];

	const fullTable = createSvelteTable({
		get data() {
			return filteredData;
		},
		get columns() {
			return fullColumns;
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
	});

	// ─── Empty table ─────────────────────────────────────────────────
	let emptySorting: SortingState = $state([]);
	let emptyPagination = $state({ pageIndex: 0, pageSize: 25 });

	const simpleColumns = fullColumns.slice(1, -1); // no select, no actions

	const emptyTable = createSvelteTable({
		data: [],
		columns: simpleColumns,
		state: {
			get sorting() {
				return emptySorting;
			},
			get pagination() {
				return emptyPagination;
			},
		},
		onSortingChange: (updater) => {
			emptySorting = typeof updater === 'function' ? updater(emptySorting) : updater;
		},
		onPaginationChange: (updater) => {
			emptyPagination = typeof updater === 'function' ? updater(emptyPagination) : updater;
		},
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});

	// ─── Paginated table (75 rows) ──────────────────────────────────
	const manyNamespaces = Array.from({ length: 75 }, (_, i) => ({
		name: `namespace-${String(i + 1).padStart(3, '0')}`,
		description: `Auto-generated namespace ${i + 1}`,
		hardQuota: `${((i % 5) + 1) * 100} GB`,
		softQuota: 80,
		storageUsed: Math.floor(Math.random() * 500_000_000_000),
		hashScheme: i % 3 === 0 ? 'MD5' : 'SHA-256',
		tags: { tag: i % 4 === 0 ? ['auto', 's3'] : ['auto'] },
	}));

	let pagSorting: SortingState = $state([]);
	let pagPagination = $state({ pageIndex: 0, pageSize: 25 });
	let pagRowSelection: Record<string, boolean> = $state({});

	const paginatedTable = createSvelteTable({
		get data() {
			return manyNamespaces;
		},
		get columns() {
			return fullColumns;
		},
		state: {
			get sorting() {
				return pagSorting;
			},
			get pagination() {
				return pagPagination;
			},
			get rowSelection() {
				return pagRowSelection;
			},
		},
		onSortingChange: (updater) => {
			pagSorting = typeof updater === 'function' ? updater(pagSorting) : updater;
		},
		onPaginationChange: (updater) => {
			pagPagination = typeof updater === 'function' ? updater(pagPagination) : updater;
		},
		onRowSelectionChange: (updater) => {
			pagRowSelection = typeof updater === 'function' ? updater(pagRowSelection) : updater;
		},
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
		enableRowSelection: true,
	});

	let pagSelectedKeys = $derived(
		Object.keys(pagRowSelection)
			.filter((k) => pagRowSelection[k])
			.map((k) => paginatedTable.getCoreRowModel().rows[Number(k)]?.original.name)
			.filter(Boolean)
	);
	let pagSelectedCount = $derived(pagSelectedKeys.length);
</script>

{#snippet nameCellSnippet(ns: Namespace)}
	<button
		type="button"
		class="text-primary underline-offset-4 hover:underline"
		onclick={(e) => {
			e.stopPropagation();
			toast.info(`Navigate to: ${ns.name}`);
		}}
	>
		{ns.name}
	</button>
{/snippet}

{#snippet storageCellSnippet(ns: Namespace)}
	{@const used = ns.storageUsed ?? 0}
	{@const quota = ns.hardQuota ? parseQuota(ns.hardQuota) : null}
	{#if used > 0 || quota}
		<div class="flex flex-col gap-1">
			<span class="text-sm">{formatBytes(used)}{quota ? ` / ${ns.hardQuota}` : ''}</span>
			{#if quota}
				{@const pct = Math.min(100, (used / quota) * 100)}
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

<Story name="Full Namespace Table">
	{#snippet template()}
		<div class="space-y-3">
			<div class="relative max-w-md">
				<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
				<Input bind:value={search} placeholder="Search namespaces..." class="pl-10" />
			</div>

			{#if selectedCount > 0}
				<div class="flex items-center gap-3 rounded-lg border bg-muted/50 px-4 py-2">
					<span class="text-sm font-medium">{selectedCount} selected</span>
					<Button
						variant="destructive"
						size="sm"
						onclick={() => toast.error(`Delete ${selectedCount} namespaces`)}
					>
						<Trash2 class="h-3.5 w-3.5" />Delete Selected
					</Button>
					<Button
						variant="outline"
						size="sm"
						onclick={() => toast.info(`Grant access to ${selectedKeys.join(', ')}`)}
					>
						<Shield class="h-3.5 w-3.5" />Grant Access
					</Button>
					<Button variant="ghost" size="sm" onclick={() => (rowSelection = {})}>Deselect All</Button
					>
				</div>
			{/if}

			<DataTable
				table={fullTable}
				onrowclick={(row) => toast.info(`Row clicked: ${row.name}`)}
				noResultsMessage={NAMESPACES.length === 0
					? 'No namespaces found. Create one to get started.'
					: `No results matching "${search}"`}
			>
				{#snippet footer()}
					{#if selectedCount > 0}
						{selectedCount} of {filteredData.length} row(s) selected.
					{/if}
				{/snippet}
			</DataTable>
		</div>
	{/snippet}
</Story>

<Story name="Empty State">
	{#snippet template()}
		<DataTable
			table={emptyTable}
			noResultsMessage="No namespaces found. Create one to get started."
		/>
	{/snippet}
</Story>

<Story name="Search Filtering">
	{#snippet template()}
		<div class="space-y-3">
			<p class="text-sm text-muted-foreground">
				Try typing "prod", "backup", "ml", or "dev" in the search box.
			</p>
			<div class="relative max-w-md">
				<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
				<Input bind:value={search} placeholder="Search namespaces..." class="pl-10" />
			</div>
			<DataTable table={fullTable} noResultsMessage={`No results matching "${search}"`} />
		</div>
	{/snippet}
</Story>

<Story name="Pagination (75 rows)">
	{#snippet template()}
		<div class="space-y-3">
			{#if pagSelectedCount > 0}
				<div class="flex items-center gap-3 rounded-lg border bg-muted/50 px-4 py-2">
					<span class="text-sm font-medium">{pagSelectedCount} selected</span>
					<Button
						variant="destructive"
						size="sm"
						onclick={() => toast.error(`Delete ${pagSelectedCount} items`)}
					>
						<Trash2 class="h-3.5 w-3.5" />Delete Selected
					</Button>
					<Button variant="ghost" size="sm" onclick={() => (pagRowSelection = {})}
						>Deselect All</Button
					>
				</div>
			{/if}

			<DataTable table={paginatedTable}>
				{#snippet footer()}
					{#if pagSelectedCount > 0}
						{pagSelectedCount} of {manyNamespaces.length} row(s) selected.
					{/if}
				{/snippet}
			</DataTable>
		</div>
	{/snippet}
</Story>

<Story name="Row Click Navigation">
	{#snippet template()}
		<p class="mb-3 text-sm text-muted-foreground">
			Click any row to see the navigation toast. Click the name link or actions menu — they stop
			propagation.
		</p>
		<DataTable
			table={fullTable}
			onrowclick={(row) => toast.info(`Navigating to /namespaces/${row.name}`)}
		/>
	{/snippet}
</Story>
