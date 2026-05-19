<script lang="ts">
	import {
		Search,
		FileSearch,
		ChevronLeft,
		ChevronRight,
		ArrowUpDown,
		ArrowUp,
		ArrowDown,
		X,
		Download,
	} from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import * as TablePrimitive from '$lib/components/ui/table/index.js';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import {
		DataTable,
		DataTableCheckbox,
		createSvelteTable,
		getCoreRowModel,
		getSortedRowModel,
		renderSnippet,
		renderComponent,
	} from '$lib/components/ui/data-table/index.js';
	import type { ColumnDef, SortingState, OnChangeFn } from '@tanstack/table-core';
	import { useSelection } from '$lib/utils/use-selection.svelte.js';
	import { formatBytes, formatDate } from '$lib/utils/format.js';
	import {
		search_objects,
		type QueryResultObject,
		type ObjectQueryResponse,
	} from '$lib/remote/search.remote.js';
	import DataTableActions from '../data-table/data-table-actions.svelte';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	// Object search state
	let objectQuery = $state('');
	let pageSize = 25;
	let objectOffset = $state(0);
	let objectResults = $state.raw<ObjectQueryResponse | null>(null);
	let objectLoading = $state(false);
	let objectError = $state('');
	let objectSearched = $state(false);

	// Sort state
	type SortField =
		| 'utf8Name'
		| 'namespace'
		| 'size'
		| 'contentType'
		| 'owner'
		| 'changeTimeMilliseconds';
	let sortField = $state<SortField | null>(null);
	let sortAsc = $state(true);

	// TanStack sorting integration
	let sorting = $derived<SortingState>(sortField ? [{ id: sortField, desc: !sortAsc }] : []);

	const handleSortingChange: OnChangeFn<SortingState> = (updater) => {
		const next = typeof updater === 'function' ? updater(sorting) : updater;
		if (next.length === 0) {
			sortField = null;
			sortAsc = true;
		} else {
			sortField = next[0].id as SortField;
			sortAsc = !next[0].desc;
		}
		if (objectSearched) {
			objectOffset = 0;
			handleObjectSearch();
		}
	};

	// Column filter state
	let filterNamespace = $state('');
	let filterContentType = $state('');
	let filterOwner = $state('');

	// Pagination
	let objectTotalPages = $derived(
		objectResults ? Math.max(1, Math.ceil(objectResults.status.totalResults / pageSize)) : 0
	);
	let objectCurrentPage = $derived(Math.floor(objectOffset / pageSize) + 1);

	// Client-side filtering of results
	let filteredResults = $derived.by(() => {
		if (!objectResults) return [];
		let items = objectResults.resultSet;
		if (filterNamespace) {
			const f = filterNamespace.toLowerCase();
			items = items.filter((i) => (i.namespace ?? '').toLowerCase().includes(f));
		}
		if (filterContentType) {
			const f = filterContentType.toLowerCase();
			items = items.filter((i) => (i.contentType ?? '').toLowerCase().includes(f));
		}
		if (filterOwner) {
			const f = filterOwner.toLowerCase();
			items = items.filter((i) => (i.owner ?? '').toLowerCase().includes(f));
		}
		return items;
	});

	let hasActiveFilters = $derived(
		filterNamespace !== '' || filterContentType !== '' || filterOwner !== ''
	);

	function displayPath(item: QueryResultObject): string {
		if (item.utf8Name) return item.utf8Name;
		try {
			return decodeURIComponent(item.urlName);
		} catch {
			return item.urlName;
		}
	}

	function formatMillis(ms: string | undefined): string {
		if (!ms) return '\u2014';
		return formatDate(new Date(Number(ms)));
	}

	function buildSortParam(): string | undefined {
		if (!sortField) return undefined;
		return `${sortAsc ? '+' : '-'}${sortField}`;
	}

	async function handleObjectSearch() {
		objectLoading = true;
		objectError = '';
		objectSearched = true;
		selected.clear();
		try {
			objectResults = await search_objects({
				tenant,
				query: objectQuery,
				count: pageSize,
				offset: objectOffset,
				verbose: true,
				sort: buildSortParam(),
			});
		} catch (err) {
			objectError = getErrorMessage(err, 'Object query failed');
			objectResults = null;
		} finally {
			objectLoading = false;
		}
	}

	function objectPagePrev() {
		objectOffset = Math.max(0, objectOffset - pageSize);
		handleObjectSearch();
	}

	function objectPageNext() {
		objectOffset += pageSize;
		handleObjectSearch();
	}

	function searchFromStart() {
		objectOffset = 0;
		handleObjectSearch();
	}

	function clearFilters() {
		filterNamespace = '';
		filterContentType = '';
		filterOwner = '';
	}

	// Selection state
	function itemKey(item: QueryResultObject): string {
		return `${item.namespace ?? ''}:${item.urlName}`;
	}

	const { selected, allSelected, toggleAll, toggleOne } = useSelection(
		() => filteredResults,
		itemKey
	);

	function objectDownloadUrl(item: QueryResultObject): string {
		const ns = item.namespace ?? '';
		return `/api/v1/buckets/${encodeURIComponent(ns)}/objects/${encodeURIComponent(item.urlName)}`;
	}

	function downloadSelected() {
		const items = filteredResults.filter((i) => selected.has(itemKey(i)));
		if (items.length === 0) return;
		for (const item of items) {
			const a = document.createElement('a');
			a.href = objectDownloadUrl(item);
			a.download = '';
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
		}
		toast.success(`Started ${items.length} download${items.length !== 1 ? 's' : ''}`);
	}

	// TanStack table
	const objectColumns: ColumnDef<QueryResultObject>[] = [
		{
			id: 'select',
			header: () =>
				renderComponent(DataTableCheckbox, {
					checked: allSelected,
					onCheckedChange: toggleAll,
					disabled: filteredResults.length === 0,
					'aria-label': 'Select all rows',
				}),
			cell: ({ row }) =>
				renderComponent(DataTableCheckbox, {
					checked: selected.has(itemKey(row.original)),
					onCheckedChange: () => toggleOne(itemKey(row.original)),
					'aria-label': `Select ${row.original.utf8Name}`,
				}),
			meta: { headerClass: 'w-10', cellClass: 'px-4 py-3' },
		},
		{
			accessorKey: 'utf8Name',
			header: ({ column }) =>
				renderSnippet(sortableHeader, {
					label: 'Path',
					sorted: column.getIsSorted(),
					onclick: () => column.toggleSorting(),
				}),
			cell: ({ row }) => renderSnippet(pathCell, row.original),
			meta: { cellClass: 'max-w-xs truncate px-4 py-3 font-medium' },
		},
		{
			accessorKey: 'namespace',
			header: ({ column }) =>
				renderSnippet(sortableHeader, {
					label: 'Namespace',
					sorted: column.getIsSorted(),
					onclick: () => column.toggleSorting(),
				}),
			cell: ({ row }) => (row.original.namespace ?? '\u2014') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			accessorKey: 'size',
			header: ({ column }) =>
				renderSnippet(sortableHeader, {
					label: 'Size',
					sorted: column.getIsSorted(),
					onclick: () => column.toggleSorting(),
				}),
			cell: ({ row }) =>
				(row.original.size != null ? formatBytes(row.original.size) : '\u2014') as string,
			meta: { headerClass: 'text-right', cellClass: 'px-4 py-3 text-right text-muted-foreground' },
		},
		{
			accessorKey: 'contentType',
			header: ({ column }) =>
				renderSnippet(sortableHeader, {
					label: 'Content Type',
					sorted: column.getIsSorted(),
					onclick: () => column.toggleSorting(),
				}),
			cell: ({ row }) => (row.original.contentType ?? '\u2014') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			accessorKey: 'owner',
			header: ({ column }) =>
				renderSnippet(sortableHeader, {
					label: 'Owner',
					sorted: column.getIsSorted(),
					onclick: () => column.toggleSorting(),
				}),
			cell: ({ row }) => (row.original.owner ?? '\u2014') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			accessorKey: 'changeTimeMilliseconds',
			header: ({ column }) =>
				renderSnippet(sortableHeader, {
					label: 'Modified',
					sorted: column.getIsSorted(),
					onclick: () => column.toggleSorting(),
				}),
			cell: ({ row }) => formatMillis(row.original.changeTimeMilliseconds) as string,
			meta: { cellClass: 'whitespace-nowrap px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) =>
				renderComponent(DataTableActions, {
					objectKey: displayPath(row.original),
					downloadUrl: objectDownloadUrl(row.original),
					namespace: row.original.namespace ?? '',
				}),
			meta: { headerClass: 'w-16', cellClass: 'px-4 py-3' },
		},
	];

	let objectTable = $derived(
		createSvelteTable({
			get data() {
				return filteredResults;
			},
			columns: objectColumns,
			getCoreRowModel: getCoreRowModel(),
			getSortedRowModel: getSortedRowModel(),
			manualSorting: true,
			state: {
				get sorting() {
					return sorting;
				},
			},
			onSortingChange: handleSortingChange,
		})
	);
</script>

{#snippet sortableHeader(props: {
	label: string;
	sorted: false | 'asc' | 'desc';
	onclick: () => void;
})}
	<Button
		variant="ghost"
		class="h-auto gap-1 p-0 hover:bg-transparent hover:text-foreground"
		onclick={props.onclick}
	>
		{props.label}
		{#if props.sorted === 'asc'}
			<ArrowUp class="h-3 w-3" />
		{:else if props.sorted === 'desc'}
			<ArrowDown class="h-3 w-3" />
		{:else}
			<ArrowUpDown class="h-3 w-3 opacity-30" />
		{/if}
	</Button>
{/snippet}

{#snippet pathCell(item: QueryResultObject)}
	<span title={displayPath(item)}>{displayPath(item)}</span>
{/snippet}

{#snippet objectSubHeaderRow()}
	<TablePrimitive.Row class="border-b bg-muted/30 hover:bg-transparent">
		<TablePrimitive.Cell class="px-4 py-1.5"></TablePrimitive.Cell>
		<TablePrimitive.Cell class="px-4 py-1.5"></TablePrimitive.Cell>
		<TablePrimitive.Cell class="px-4 py-1.5">
			<Input bind:value={filterNamespace} placeholder="Filter..." class="h-6 px-2 text-xs" />
		</TablePrimitive.Cell>
		<TablePrimitive.Cell class="px-4 py-1.5"></TablePrimitive.Cell>
		<TablePrimitive.Cell class="px-4 py-1.5">
			<Input bind:value={filterContentType} placeholder="Filter..." class="h-6 px-2 text-xs" />
		</TablePrimitive.Cell>
		<TablePrimitive.Cell class="px-4 py-1.5">
			<Input bind:value={filterOwner} placeholder="Filter..." class="h-6 px-2 text-xs" />
		</TablePrimitive.Cell>
		<TablePrimitive.Cell class="px-4 py-1.5"></TablePrimitive.Cell>
		<TablePrimitive.Cell class="px-4 py-1.5"></TablePrimitive.Cell>
	</TablePrimitive.Row>
{/snippet}

<!-- Object search form -->
<div class="flex flex-wrap items-end gap-3">
	<div class="min-w-0 flex-1">
		<Label for="obj-query" class="sr-only">Query</Label>
		<div class="relative">
			<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
			<Input
				id="obj-query"
				bind:value={objectQuery}
				placeholder="*:*"
				class="pl-9"
				onkeydown={(e) => e.key === 'Enter' && searchFromStart()}
			/>
		</div>
	</div>
	<Button onclick={searchFromStart} disabled={objectLoading}>
		<Search class="h-4 w-4" />
		{objectLoading ? 'Searching...' : 'Search'}
	</Button>
</div>

<!-- Error -->
<ErrorBanner message={objectError} />

<!-- Results -->
{#if objectLoading}
	<TableSkeleton rows={5} columns={6} />
{:else if objectResults && objectResults.resultSet.length > 0}
	<div class="flex items-center justify-between text-sm text-muted-foreground">
		<span>
			{#if hasActiveFilters}
				{filteredResults.length} filtered /
			{/if}
			Showing {objectOffset + 1}–{Math.min(
				objectOffset + objectResults.resultSet.length,
				objectResults.status.totalResults
			)} of {objectResults.status.totalResults.toLocaleString()} results
		</span>
		{#if hasActiveFilters}
			<Button variant="link" size="sm" class="h-auto gap-1 p-0 text-xs" onclick={clearFilters}>
				<X class="h-3 w-3" />
				Clear filters
			</Button>
		{/if}
	</div>

	{#if selected.size > 0}
		<div class="flex items-center gap-3 rounded-lg border bg-muted/50 px-4 py-2">
			<span class="text-sm font-medium">{selected.size} selected</span>
			<Button size="sm" onclick={downloadSelected}>
				<Download class="h-3.5 w-3.5" />Download Selected
			</Button>
			<Button variant="ghost" size="sm" onclick={() => selected.clear()}>Deselect All</Button>
		</div>
	{/if}

	<DataTable
		table={objectTable}
		noResultsMessage="No results match your filters."
		subHeaderRow={objectSubHeaderRow}
	/>

	<!-- Pagination -->
	{#if objectTotalPages > 1}
		<div class="flex items-center justify-center gap-4">
			<Button variant="outline" size="sm" onclick={objectPagePrev} disabled={objectOffset === 0}>
				<ChevronLeft class="h-4 w-4" />
				Previous
			</Button>
			<span class="text-sm text-muted-foreground">
				Page {objectCurrentPage} of {objectTotalPages}
			</span>
			<Button
				variant="outline"
				size="sm"
				onclick={objectPageNext}
				disabled={objectCurrentPage >= objectTotalPages}
			>
				Next
				<ChevronRight class="h-4 w-4" />
			</Button>
		</div>
	{/if}
{:else if objectSearched && !objectError}
	<div class="rounded-lg border border-dashed p-8 text-center">
		<FileSearch class="mx-auto h-10 w-10 text-muted-foreground/50" />
		<p class="mt-2 text-muted-foreground">No objects found matching your query.</p>
	</div>
{:else if !objectSearched}
	<div class="rounded-lg border border-dashed p-8 text-center">
		<Search class="mx-auto h-10 w-10 text-muted-foreground/50" />
		<p class="mt-2 text-muted-foreground">Enter a query and click Search to find objects.</p>
	</div>
{/if}
