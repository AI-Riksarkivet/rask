<script lang="ts">
	// `/lineage/datasets` — the governed lineage catalog on the shared @rask/ui DataTable
	// (Marquez-parity list view): sortable columns, a text filter, a namespace filter, and the
	// governed server-side SearchBar (name / namespace / tag / COLUMN hits with why-chips).
	// Row-click → the per-dataset detail page.
	import {
		createSvelteTable,
		DataTable,
		DataTableHeaderButton,
		DataTableTextFilter,
		getCoreRowModel,
		getFilteredRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderComponent,
		renderSnippet,
		type ColumnDef,
		type PaginationState,
		type SortingState,
	} from '@rask/ui/data-table';
	import { Select } from '@rask/ui/select';
	import { SearchBar } from '@rask/ui/search-bar';
	import { RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { fetchSearch, listDatasets } from '$lib/api';
	import type { DatasetSummary } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let datasets = $state<DatasetSummary[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false); // "still loading" (0, unsettled) vs a network error (0, settled)
	let lastUpdated = $state(''); // when the rows on screen were last confirmed live

	const unauthorized = $derived(datasets === null && lastStatus === 401);
	const offline = $derived(datasets === null && settled && lastStatus !== 401);
	// A later poll failed while rows are on screen: the table is a last-good snapshot, not live —
	// say so (the graph page's store tracks online/lastUpdated for the same reason).
	const stale = $derived(datasets !== null && settled && lastStatus !== 200);

	async function load(): Promise<void> {
		const res = await listDatasets();
		settled = true;
		if (res.ok) {
			datasets = res.data.datasets ?? [];
			lastStatus = 200;
			lastUpdated = new Date().toLocaleTimeString();
		} else if (res.status === 401) {
			// Session expired mid-view: clear the rows so the sign-in state shows — an expired
			// session must not keep rendering the governed list (the admin audit viewer's rule).
			datasets = null;
			lastStatus = 401;
		} else {
			lastStatus = res.status; // keep the last-good rows; the stale banner names the failure
		}
	}

	// A dataset appears, gains a version or fails only via a lineage event, so the cursor is the exact
	// trigger. A transient failure keeps the last-good rows and re-reads on the next advance.
	liveRead(lineageTick, () => load());

	const rows = $derived.by((): DatasetSummary[] => {
		const all = datasets ?? [];
		return nsFilter ? all.filter((d) => d.namespace === nsFilter) : all;
	});
	const namespaces = $derived(
		[
			...new Set((datasets ?? []).map((d) => d.namespace).filter((ns): ns is string => !!ns)),
		].sort(),
	);

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });
	let nsFilter = $state(''); // '' = all namespaces

	const columns: ColumnDef<DatasetSummary>[] = [
		{
			id: 'dataset',
			accessorKey: 'name',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'dataset',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nameCell, row.original),
		},
		{
			id: 'namespace',
			accessorFn: (d) => d.namespace ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'namespace',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nsCell, row.original),
			meta: { headerClass: 'w-44' },
		},
		{
			id: 'tags',
			accessorFn: (d) => (d.tags ?? []).join(' '),
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'tags',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(tagsCell, row.original),
		},
	];

	const table = createSvelteTable({
		get data() {
			return rows;
		},
		columns,
		state: {
			get sorting() {
				return sorting;
			},
			get globalFilter() {
				return globalFilter;
			},
			get pagination() {
				return pagination;
			},
		},
		onSortingChange: (u) => (sorting = typeof u === 'function' ? u(sorting) : u),
		onGlobalFilterChange: (u) => (globalFilter = typeof u === 'function' ? u(globalFilter) : u),
		onPaginationChange: (u) => (pagination = typeof u === 'function' ? u(pagination) : u),
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getFilteredRowModel: getFilteredRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});
</script>

{#snippet nameCell(row: DatasetSummary)}
	<a class="rowlink mono" href={`${base}/lineage/datasets/${encodeURIComponent(row.name)}`}
		>{row.name}</a
	>
{/snippet}
{#snippet nsCell(row: DatasetSummary)}
	{#if row.namespace}<span class="ns">{row.namespace}</span>{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet tagsCell(row: DatasetSummary)}
	{#if (row.tags ?? []).length}
		<span class="tags">
			{#each row.tags ?? [] as t (t)}<span class="tag">{t}</span>{/each}
		</span>
	{:else}<span class="mut">—</span>{/if}
{/snippet}

<svelte:head><title>Datasets · lineage · rask</title></svelte:head>

<div class="page">
	<header>
		<h1>Datasets</h1>
		<span class="sub"
			>every dataset the lineage graph knows — governed, so only what you may see</span
		>
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to browse lineage.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Lineage service unreachable (HTTP {lastStatus}) — retrying.</p>
		</div>
	{:else}
		{#if stale}
			<div class="stale" role="status">
				<RefreshCw size={13} />
				<span>
					Lineage service unreachable (HTTP {lastStatus}) — showing the last snapshot{lastUpdated
						? ` from ${lastUpdated}`
						: ''}, retrying.
				</span>
			</div>
		{/if}
		<!-- Server-side GOVERNED search: reaches tags + the column inventory the client-side
		     filter below can't see; hits show WHY they matched (e.g. column:embedding). -->
		<div class="searchbar">
			<SearchBar
				search={async (q) => (await fetchSearch(q))?.results ?? []}
				onselect={(name) => goto(`${base}/lineage/datasets/${encodeURIComponent(name)}`)}
			/>
		</div>
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Filter datasets…" />
			<Select
				bind:value={nsFilter}
				ariaLabel="Namespace filter"
				placeholder="all namespaces"
				options={[
					{ value: '', label: 'all namespaces' },
					...namespaces.map((ns) => ({ value: ns, label: ns })),
				]}
			/>
		</div>
		<DataTable
			{table}
			loading={datasets === null}
			emptyMessage="No datasets yet — they appear as pipelines emit OpenLineage events."
		/>
	{/if}
</div>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
		width: 100%;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 12px;
		margin-bottom: 18px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.searchbar {
		margin-bottom: 10px;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	.rowlink {
		color: var(--ink);
		text-decoration: none;
	}
	.rowlink:hover {
		text-decoration: underline;
	}
	.ns {
		font-size: 11px;
		padding: 1px 8px;
		border-radius: 999px;
		background: color-mix(in srgb, var(--accent) 16%, transparent);
		color: var(--ink);
	}
	.tags {
		display: inline-flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.tag {
		font-size: 10px;
		padding: 1px 6px;
		border-radius: 999px;
		border: 1px solid var(--line);
		color: var(--mut);
	}
	.mut {
		color: var(--faint);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
	.stale {
		display: flex;
		align-items: center;
		gap: 8px;
		border: 1px solid color-mix(in srgb, var(--warn, #d18b28) 45%, var(--line));
		border-radius: 6px;
		color: var(--warn, #d18b28);
		font-size: 12px;
		padding: 6px 10px;
		margin-bottom: 10px;
	}
</style>
