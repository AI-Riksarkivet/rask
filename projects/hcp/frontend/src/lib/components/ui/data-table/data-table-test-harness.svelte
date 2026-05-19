<script lang="ts">
	import { Search, Trash2, Shield } from 'lucide-svelte';
	import {
		createSvelteTable,
		getCoreRowModel,
		getSortedRowModel,
		getPaginationRowModel,
		renderComponent,
	} from './index.js';
	import DataTable from './data-table.svelte';
	import DataTableHeaderButton from './data-table-header-button.svelte';
	import DataTableCheckbox from './data-table-checkbox.svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import type { ColumnDef, SortingState, PaginationState } from '@tanstack/table-core';

	const NAMESPACES = [
		{
			name: 'production-data',
			description: 'Main production namespace',
			hardQuota: '500 GB',
			hashScheme: 'SHA-256',
		},
		{
			name: 'staging-env',
			description: 'Staging environment',
			hardQuota: '100 GB',
			hashScheme: 'SHA-256',
		},
		{
			name: 'dev-sandbox',
			description: 'Developer sandbox',
			hardQuota: '50 GB',
			hashScheme: 'MD5',
		},
		{
			name: 'analytics-warehouse',
			description: 'Analytics data lake',
			hardQuota: '1 TB',
			hashScheme: 'SHA-256',
		},
		{
			name: 'backup-vault',
			description: 'Encrypted backups',
			hardQuota: '2 TB',
			hashScheme: 'SHA-512',
		},
		{
			name: 'media-assets',
			description: 'Images and videos',
			hardQuota: '200 GB',
			hashScheme: 'SHA-256',
		},
		{
			name: 'compliance-archive',
			description: 'Regulatory compliance data',
			hardQuota: '500 GB',
			hashScheme: 'SHA-256',
		},
		{
			name: 'ml-training',
			description: 'Machine learning datasets',
			hardQuota: '1 TB',
			hashScheme: 'SHA-256',
		},
		{
			name: 'logs-archive',
			description: 'Centralized log storage',
			hardQuota: '300 GB',
			hashScheme: 'MD5',
		},
		{
			name: 'shared-assets',
			description: 'Cross-team shared files',
			hardQuota: '100 GB',
			hashScheme: 'SHA-256',
		},
	];

	type Namespace = (typeof NAMESPACES)[number];

	let search = $state('');
	let filteredData = $derived(
		NAMESPACES.filter((n) => n.name.toLowerCase().includes(search.toLowerCase()))
	);

	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });
	let rowSelection = $state<Record<string, boolean>>({});

	let selectedKeys = $derived(
		Object.keys(rowSelection)
			.filter((k) => rowSelection[k])
			.map((k) => table.getCoreRowModel().rows[Number(k)]?.original.name)
			.filter(Boolean) as string[]
	);
	let selectedCount = $derived(selectedKeys.length);

	const columns: ColumnDef<Namespace>[] = [
		{
			id: 'select',
			header: ({ table: t }) =>
				renderComponent(DataTableCheckbox, {
					checked: t.getIsAllPageRowsSelected(),
					onCheckedChange: (val: boolean) => t.toggleAllPageRowsSelected(!!val),
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
			meta: { cellClass: 'px-4 py-3 font-medium' },
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
			header: 'Hard Quota',
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'hashScheme',
			header: 'Hash Scheme',
			cell: ({ row }) => row.original.hashScheme,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return filteredData;
		},
		columns,
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
</script>

<div class="space-y-3">
	<div class="relative max-w-md">
		<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
		<Input
			bind:value={search}
			placeholder="Search namespaces..."
			class="pl-10"
			aria-label="Search namespaces"
		/>
	</div>

	{#if selectedCount > 0}
		<div class="flex items-center gap-3 rounded-lg border bg-muted/50 px-4 py-2">
			<span class="text-sm font-medium">{selectedCount} selected</span>
			<Button variant="destructive" size="sm">
				<Trash2 class="h-3.5 w-3.5" />Delete Selected
			</Button>
			<Button variant="outline" size="sm">
				<Shield class="h-3.5 w-3.5" />Grant Access
			</Button>
			<Button variant="ghost" size="sm" onclick={() => (rowSelection = {})}>Deselect All</Button>
		</div>
	{/if}

	<DataTable {table} noResultsMessage={`No results matching "${search}"`}>
		{#snippet footer()}
			{#if selectedCount > 0}
				{selectedCount} of {filteredData.length} row(s) selected.
			{/if}
		{/snippet}
	</DataTable>
</div>
