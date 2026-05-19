<script lang="ts">
	import { Activity } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import {
		DataTable,
		createSvelteTable,
		getCoreRowModel,
		renderSnippet,
	} from '$lib/components/ui/data-table/index.js';
	import type { ColumnDef } from '@tanstack/table-core';
	import { formatDate } from '$lib/utils/format.js';
	import {
		search_operations,
		type QueryResultObject,
		type OperationQueryResponse,
	} from '$lib/remote/search.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let opTransactions = $state<string[]>([]);
	let opNamespace = $state('');
	let opResults = $state.raw<OperationQueryResponse | null>(null);
	let opLoading = $state(false);
	let opError = $state('');
	let opSearched = $state(false);

	let sortedOpResults = $derived.by(() => {
		if (!opResults) return [];
		return [...opResults.resultSet].sort(
			(a, b) => Number(b.changeTimeMilliseconds ?? 0) - Number(a.changeTimeMilliseconds ?? 0)
		);
	});

	function formatMillis(ms: string | undefined): string {
		if (!ms) return '\u2014';
		return formatDate(new Date(Number(ms)));
	}

	type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'outline' | 'success' | 'warning';
	function operationVariant(op: string): BadgeVariant {
		switch (op.toUpperCase()) {
			case 'CREATED':
				return 'success';
			case 'DELETED':
				return 'destructive';
			case 'PURGED':
				return 'warning';
			default:
				return 'secondary';
		}
	}

	function displayPath(item: QueryResultObject): string {
		if (item.utf8Name) return item.utf8Name;
		try {
			return decodeURIComponent(item.urlName);
		} catch {
			return item.urlName;
		}
	}

	async function handleOpSearch() {
		opLoading = true;
		opError = '';
		opSearched = true;
		try {
			opResults = await search_operations({
				tenant,
				count: 100,
				verbose: true,
				transactions: opTransactions.length > 0 ? opTransactions : undefined,
				namespaces: opNamespace ? [opNamespace] : undefined,
			});
		} catch (err) {
			opError = getErrorMessage(err, 'Operation query failed');
			opResults = null;
		} finally {
			opLoading = false;
		}
	}

	function searchFromStart() {
		handleOpSearch();
	}

	function toggleTransaction(tx: string) {
		if (opTransactions.includes(tx)) {
			opTransactions = opTransactions.filter((t) => t !== tx);
		} else {
			opTransactions = [...opTransactions, tx];
		}
	}

	// TanStack table
	const opColumns: ColumnDef<QueryResultObject>[] = [
		{
			accessorKey: 'utf8Name',
			header: 'Path',
			cell: ({ row }) => renderSnippet(pathCell, row.original),
			meta: { cellClass: 'max-w-xs truncate px-4 py-3 font-medium' },
		},
		{
			accessorKey: 'operation',
			header: 'Operation',
			cell: ({ row }) => renderSnippet(operationBadge, row.original),
		},
		{
			accessorKey: 'namespace',
			header: 'Namespace',
			cell: ({ row }) => (row.original.namespace ?? '\u2014') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			accessorKey: 'changeTimeMilliseconds',
			header: 'Time',
			cell: ({ row }) => formatMillis(row.original.changeTimeMilliseconds) as string,
			meta: { cellClass: 'whitespace-nowrap px-4 py-3 text-muted-foreground' },
		},
	];

	let opTable = $derived(
		createSvelteTable({
			get data() {
				return sortedOpResults;
			},
			columns: opColumns,
			getCoreRowModel: getCoreRowModel(),
		})
	);
</script>

{#snippet pathCell(item: QueryResultObject)}
	<span title={displayPath(item)}>{displayPath(item)}</span>
{/snippet}

{#snippet operationBadge(item: QueryResultObject)}
	<Badge variant={operationVariant(item.operation)}>
		{item.operation}
	</Badge>
{/snippet}

<!-- Operation search form -->
<div class="flex flex-wrap items-end gap-4">
	<div>
		<Label>Operation type</Label>
		<div class="mt-1.5 flex gap-4">
			{#each ['create', 'delete', 'purge'] as tx (tx)}
				<label class="flex items-center gap-2 text-sm">
					<Checkbox
						checked={opTransactions.includes(tx)}
						onCheckedChange={() => toggleTransaction(tx)}
					/>
					<span class="capitalize">{tx}</span>
				</label>
			{/each}
		</div>
	</div>
	<div class="min-w-0">
		<Label for="op-ns">Namespace</Label>
		<Input
			id="op-ns"
			bind:value={opNamespace}
			placeholder="All namespaces"
			class="mt-1.5 w-48"
			onkeydown={(e) => e.key === 'Enter' && handleOpSearch()}
		/>
	</div>
	<Button onclick={handleOpSearch} disabled={opLoading}>
		<Activity class="h-4 w-4" />
		{opLoading ? 'Searching...' : 'Search'}
	</Button>
</div>

<!-- Error -->
<ErrorBanner message={opError} />

<!-- Results -->
{#if opLoading}
	<TableSkeleton rows={5} columns={4} />
{:else if opResults && sortedOpResults.length > 0}
	<div class="text-sm text-muted-foreground">
		{opResults.status.totalResults.toLocaleString()} operations found
	</div>
	<DataTable table={opTable} noResultsMessage="No operations found." />
{:else if opSearched && !opError}
	<div class="rounded-lg border border-dashed p-8 text-center">
		<Activity class="mx-auto h-10 w-10 text-muted-foreground/50" />
		<p class="mt-2 text-muted-foreground">No operations found.</p>
	</div>
{:else if !opSearched}
	<div class="rounded-lg border border-dashed p-8 text-center">
		<Activity class="mx-auto h-10 w-10 text-muted-foreground/50" />
		<p class="mt-2 text-muted-foreground">
			Select operation types and click Search to view audit events.
		</p>
	</div>
{/if}
