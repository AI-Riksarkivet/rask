<script lang="ts">
	// `/lineage/jobs` — the governed compute identities on the shared @rask/ui DataTable
	// (Marquez-parity list view). A job is listed only if the caller may see every dataset it
	// wrote. Row-click → the per-job detail page (`/jobs/<namespace>/<name>`, a rest route).
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
	import { RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { listJobs } from '$lib/api';
	import type { JobSummary } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let jobs = $state<JobSummary[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);

	const unauthorized = $derived(jobs === null && lastStatus === 401);
	const offline = $derived(jobs === null && settled && lastStatus !== 401);

	async function load(): Promise<void> {
		const res = await listJobs();
		settled = true;
		if (res.ok) {
			jobs = res.data.jobs ?? [];
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	// Jobs are folded from lineage events, so the cursor is the exact trigger for this list.
	liveRead(lineageTick, () => load());

	// Job identity is `namespace/name` — the detail route is a rest segment, so encode per part.
	// A namespace-less job (nullable on the wire) is addressed by bare name.
	const jobHref = (j: JobSummary) =>
		`${base}/lineage/jobs/${j.namespace ? `${encodeURIComponent(j.namespace)}/` : ''}${encodeURIComponent(j.name)}`;

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	const columns: ColumnDef<JobSummary>[] = [
		{
			id: 'job',
			accessorKey: 'name',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'job',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nameCell, row.original),
		},
		{
			id: 'namespace',
			accessorKey: 'namespace',
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
			id: 'writes',
			accessorFn: (j) => (j.outputs ?? []).join(' '),
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'writes',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(outputsCell, row.original),
		},
	];

	const table = createSvelteTable({
		get data() {
			return jobs ?? [];
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

{#snippet nameCell(row: JobSummary)}
	<a class="rowlink mono" href={jobHref(row)}>{row.name}</a>
{/snippet}
{#snippet nsCell(row: JobSummary)}
	{#if row.namespace}<span class="ns">{row.namespace}</span>{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet outputsCell(row: JobSummary)}
	{#if (row.outputs ?? []).length}
		<span class="tags">
			{#each row.outputs ?? [] as out (out)}
				<a class="tag mono" href={`${base}/lineage/datasets/${encodeURIComponent(out)}`}>{out}</a>
			{/each}
		</span>
	{:else}<span class="mut">—</span>{/if}
{/snippet}

<svelte:head><title>Jobs · lineage · rask</title></svelte:head>

<div class="page">
	<header>
		<h1>Jobs</h1>
		<span class="sub">every compute identity that has run — governed by the datasets it wrote</span>
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
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Filter jobs…" />
		</div>
		<DataTable
			{table}
			loading={jobs === null}
			emptyMessage="No jobs recorded yet — they appear as pipelines emit OpenLineage events."
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
		text-decoration: none;
	}
	.tag:hover {
		color: var(--ink);
		border-color: var(--accent);
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
</style>
