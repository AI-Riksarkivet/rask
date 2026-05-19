<script lang="ts">
	import { goto } from '$app/navigation';
	import { Search } from 'lucide-svelte';
	import * as Card from '$lib/components/ui/card/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import {
		DataTable,
		DataTableHeaderButton,
		createSvelteTable,
		getCoreRowModel,
		getSortedRowModel,
		getPaginationRowModel,
		renderSnippet,
		renderComponent,
	} from '$lib/components/ui/data-table/index.js';
	import type { ColumnDef, SortingState, PaginationState } from '@tanstack/table-core';
	import { formatBytes } from '$lib/utils/format.js';
	import type { ChargebackEntry } from '$lib/utils/format.js';
	import type { ChargebackReport } from '$lib/remote/tenant-info.remote.js';
	import type { RemoteQuery } from '@sveltejs/kit';

	let {
		chargebackData,
	}: {
		chargebackData: RemoteQuery<ChargebackReport>;
	} = $props();

	// Aggregate chargeback entries by namespace (HCP may return multiple rows per namespace)
	let entries = $derived.by(() => {
		const raw = (chargebackData?.current?.chargebackData ?? []) as ChargebackEntry[];
		const map = new Map<string, ChargebackEntry>();
		for (const e of raw) {
			const name = e.namespaceName ?? '';
			const existing = map.get(name);
			if (!existing) {
				map.set(name, { ...e });
			} else {
				existing.objectCount = Math.max(existing.objectCount ?? 0, e.objectCount ?? 0);
				existing.storageCapacityUsed = Math.max(
					existing.storageCapacityUsed ?? 0,
					e.storageCapacityUsed ?? 0
				);
				existing.ingestedVolume = (existing.ingestedVolume ?? 0) + (e.ingestedVolume ?? 0);
				existing.bytesIn = (existing.bytesIn ?? 0) + (e.bytesIn ?? 0);
				existing.bytesOut = (existing.bytesOut ?? 0) + (e.bytesOut ?? 0);
				existing.reads = (existing.reads ?? 0) + (e.reads ?? 0);
				existing.writes = (existing.writes ?? 0) + (e.writes ?? 0);
				existing.deletes = (existing.deletes ?? 0) + (e.deletes ?? 0);
			}
		}
		return [...map.values()];
	});

	// Search + filtered data
	let search = $state('');
	let filteredEntries = $derived(
		entries.filter((e) => {
			const q = search.toLowerCase();
			if (!q) return true;
			return (e.namespaceName ?? '').toLowerCase().includes(q);
		})
	);

	// Totals (computed from all entries, not filtered)
	function sumColumn(key: keyof ChargebackEntry): number {
		return entries.reduce((acc, e) => acc + (Number(e[key]) || 0), 0);
	}

	let totalStorage = $derived(formatBytes(sumColumn('storageCapacityUsed')));
	let totalObjects = $derived(sumColumn('objectCount').toLocaleString());

	// TanStack state
	let sorting = $state<SortingState>([{ id: 'storageCapacityUsed', desc: true }]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });

	let columns = $derived.by((): ColumnDef<ChargebackEntry>[] => [
		{
			accessorKey: 'namespaceName',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Namespace',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(namespaceCell, row.original),
		},
		{
			accessorKey: 'objectCount',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Objects',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => (Number(row.original.objectCount) || 0).toLocaleString(),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
		{
			accessorKey: 'ingestedVolume',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Ingested Volume',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => formatBytes(Number(row.original.ingestedVolume) || 0),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
		{
			accessorKey: 'storageCapacityUsed',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Storage Used',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => formatBytes(Number(row.original.storageCapacityUsed) || 0),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
		{
			accessorKey: 'bytesIn',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Bytes In',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => formatBytes(Number(row.original.bytesIn) || 0),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
		{
			accessorKey: 'bytesOut',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Bytes Out',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => formatBytes(Number(row.original.bytesOut) || 0),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
		{
			accessorKey: 'reads',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Reads',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => (Number(row.original.reads) || 0).toLocaleString(),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
		{
			accessorKey: 'writes',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Writes',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => (Number(row.original.writes) || 0).toLocaleString(),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
		{
			accessorKey: 'deletes',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Deletes',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => (Number(row.original.deletes) || 0).toLocaleString(),
			meta: { cellClass: 'text-right', headerClass: 'text-right' },
		},
	]);

	let table = $derived(
		createSvelteTable({
			get data() {
				return filteredEntries;
			},
			get columns() {
				return columns;
			},
			state: {
				get sorting() {
					return sorting;
				},
				get pagination() {
					return pagination;
				},
			},
			onSortingChange: (updater) => {
				sorting = typeof updater === 'function' ? updater(sorting) : updater;
			},
			onPaginationChange: (updater) => {
				pagination = typeof updater === 'function' ? updater(pagination) : updater;
			},
			getCoreRowModel: getCoreRowModel(),
			getSortedRowModel: getSortedRowModel(),
			getPaginationRowModel: getPaginationRowModel(),
		})
	);
</script>

{#snippet namespaceCell(entry: ChargebackEntry)}
	<a
		href="/namespaces/{entry.namespaceName}"
		class="text-primary underline-offset-4 hover:underline"
	>
		{entry.namespaceName}
	</a>
{/snippet}

{#snippet totalsFooter()}
	<span class="text-sm font-medium">
		Total: {totalStorage} stored across {totalObjects} objects
	</span>
{/snippet}

{#await chargebackData}
	<Card.Root>
		<Card.Header>
			<Card.Title>Namespace Chargeback</Card.Title>
			<Card.Description>Storage and I/O breakdown per namespace</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-4">
			<TableSkeleton rows={5} columns={9} />
		</Card.Content>
	</Card.Root>
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Namespace Chargeback</Card.Title>
			<Card.Description>Storage and I/O breakdown per namespace</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-4">
			<div class="relative max-w-md">
				<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
				<Input bind:value={search} placeholder="Search namespaces..." class="pl-10" />
			</div>
			<DataTable
				{table}
				onrowclick={(entry) => goto(`/namespaces/${entry.namespaceName}`)}
				noResultsMessage="No chargeback data available."
				footer={totalsFooter}
			/>
		</Card.Content>
	</Card.Root>
{/await}
