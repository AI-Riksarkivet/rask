<!--
	The RUN BOARD — rendered by BOTH `/lakehouse/lineage/runs` and the dock's RunsPanel.

	It existed twice before: this view, and a 39-line hand-written list in the panel. They drifted
	at once — the page sorts, filters, paginates and drills in; the panel showed a badge and an id.
-->
<script lang="ts">
	// ROWS ARRIVE AS A PROP, and that is the whole point of this component existing.
	//
	// The route fetches with `listRuns` on the lineage cursor. The DOCK's panel reads the same rows
	// out of the shared `LineageState` that also feeds the graph and the event feed — one poll for
	// three panels, which is the property that justifies an in-zone dock at all. Extracting a board
	// that did its OWN fetching would have re-mirrored the view and broken that in one move.
	//
	// So: this file owns PRESENTATION (sort, filter, pagination, the drill-in selection). The caller
	// owns where the rows came from and whether the read failed.
	let {
		runs = null,
		unauthorized = false,
		offline = false,
		loginHref = '/auth/login',
	}: {
		runs?: RunStatus[] | null;
		unauthorized?: boolean;
		offline?: boolean;
		loginHref?: string;
	} = $props();

	// `/lineage/runs` — the durable run-status board on the shared @rask/ui DataTable
	// (Marquez-parity list view): each run's folded current state, sortable/filterable, newest
	// first. Clicking a row opens the run's drill-in below the table (error, outputs, and the
	// reproducibility pins — the per-run /inputs read stays off the polled board, N+1 guard).
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
	import { enter } from '@rask/ui/motion';
	import { listRuns } from '$lib/api';
	import RunInputs from '$lib/lineage/RunInputs.svelte';
	import type { RunStatus } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	let selectedId = $state<string | null>(null);

	const selected = $derived((runs ?? []).find((r) => r.run_id === selectedId) ?? null);

	// Newest first by default — the board is a feed.
	let sorting = $state<SortingState>([{ id: 'updated', desc: true }]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	const jobHref = (job: string) =>
		`${base}/lineage/jobs/${job.split('/').map(encodeURIComponent).join('/')}`;

	const columns: ColumnDef<RunStatus>[] = [
		{
			id: 'state',
			accessorFn: (r) => r.state ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'state',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(stateCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'run',
			accessorKey: 'run_id',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'run',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(runCell, row.original),
		},
		{
			id: 'job',
			accessorFn: (r) => r.job ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'job',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(jobCell, row.original),
		},
		{
			id: 'author',
			accessorFn: (r) => r.author ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'author',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(authorCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'updated',
			accessorFn: (r) => r.updated_at ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'updated',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(updatedCell, row.original),
			meta: { headerClass: 'w-44' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return runs ?? [];
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

	const stateColor = (s?: string | null) =>
		/FAIL|ABORT/i.test(s ?? '') ? 'var(--fail)' : s === 'COMPLETE' ? 'var(--ok)' : 'var(--amber)';
</script>

{#snippet stateCell(row: RunStatus)}
	<span class="badge" style:background={stateColor(row.state)}>
		{row.state}{#if row.progress_total}&nbsp;{row.progress_done ?? 0}/{row.progress_total}{/if}
	</span>
{/snippet}
{#snippet runCell(row: RunStatus)}
	<button
		class="runbtn mono"
		class:on={selectedId === row.run_id}
		onclick={() => (selectedId = selectedId === row.run_id ? null : row.run_id)}
		title="Open run drill-in"
	>
		{row.run_id}
	</button>
{/snippet}
{#snippet jobCell(row: RunStatus)}
	{#if row.job}
		<a class="joblink mono" href={jobHref(row.job)}>{row.job}</a>
	{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet authorCell(row: RunStatus)}
	<span class="mut">{row.author ?? '—'}</span>
{/snippet}
{#snippet updatedCell(row: RunStatus)}
	<span class="mut mono ts">{row.updated_at ?? '—'}</span>
{/snippet}

<div class="page">
	<header>
		<h1>Runs</h1>
		<span class="sub">the durable run board — each run's current state, folded last-wins</span>
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
			<p>Lineage service unreachable — retrying.</p>
		</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Filter runs…" />
		</div>
		<DataTable
			{table}
			loading={runs === null}
			emptyMessage="No runs yet — the board fills as pipelines emit run events."
		/>

		{#if selected}
			<div class="drill" {@attach enter({ y: 6 })}>
				<div class="drill-top">
					<span class="badge" style:background={stateColor(selected.state)}>{selected.state}</span>
					<span class="mono runid">{selected.run_id}</span>
					<span class="mut">{selected.events} event{selected.events === 1 ? '' : 's'}</span>
					<button class="close" aria-label="Close run drill-in" onclick={() => (selectedId = null)}
						>×</button
					>
				</div>
				{#if (selected.outputs ?? []).length}
					<div class="outs">
						<span class="rel-label">Wrote</span>
						{#each selected.outputs ?? [] as out (out)}
							<a class="rel-chip mono" href={`${base}/lineage/datasets/${encodeURIComponent(out)}`}
								>{out}</a
							>
						{/each}
					</div>
				{/if}
				{#if selected.error_message}<div class="err">{selected.error_message}</div>{/if}
				<!-- The reproducibility pins (#115 D1): the inputs this run consumed at their exact versions. -->
				<RunInputs runId={selected.run_id} />
			</div>
		{/if}
	{/if}
</div>

<style>
	.page {
		max-width: 980px;
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
	.badge {
		display: inline-block;
		padding: 1px 8px;
		border-radius: 999px;
		font-size: 11px;
		font-weight: 700;
		color: #06210f;
		white-space: nowrap;
	}
	.runbtn {
		background: none;
		border: none;
		padding: 0;
		color: var(--ink);
		font-size: 12px;
		cursor: pointer;
		word-break: break-all;
		text-align: left;
	}
	.runbtn:hover,
	.runbtn.on {
		text-decoration: underline;
	}
	.joblink {
		color: var(--mut);
		text-decoration: none;
	}
	.joblink:hover {
		color: var(--ink);
	}
	.mut {
		color: var(--faint);
	}
	.ts {
		font-size: 11px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
	.drill {
		margin-top: 12px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 10px 12px;
		font-size: 12px;
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
	}
	.drill-top {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.runid {
		word-break: break-all;
	}
	.close {
		margin-left: auto;
		border: none;
		background: transparent;
		color: var(--mut);
		font-size: 16px;
		line-height: 1;
		cursor: pointer;
	}
	.close:hover {
		color: var(--ink);
	}
	.outs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
		margin-top: 8px;
	}
	.rel-label {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		color: var(--mut);
	}
	.rel-chip {
		font-size: 11px;
		padding: 2px 8px;
		border-radius: 999px;
		border: 1px solid var(--line);
		background: var(--panel);
		color: var(--ink);
		text-decoration: none;
	}
	.rel-chip:hover {
		border-color: var(--accent);
	}
	.err {
		color: var(--fail);
		margin-top: 6px;
	}
</style>
