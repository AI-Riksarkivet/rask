<script lang="ts">
	// Per-model detail for the registry (goal cond 9): the candidate-vs-blessed metrics comparison,
	// the model's ARTIFACTS (the describe response's new `artifacts` listing) on the shared
	// DataTable, and the training curves plotted from the experiments BFF. Rendered inside the
	// registry's expanded row; the promote flow stays on the row itself, unchanged.
	import {
		createSvelteTable,
		DataTable,
		DataTableHeaderButton,
		getCoreRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderComponent,
		renderSnippet,
		type ColumnDef,
		type PaginationState,
		type SortingState,
	} from '@rask/ui/data-table';
	import type { ModelArtifact, ModelDescribe } from './catalog';
	import TrainingCurves from './TrainingCurves.svelte';

	let { model, detail }: { model: string; detail: ModelDescribe } = $props();

	function metricNames(d: ModelDescribe): string[] {
		return Object.keys({ ...d.candidate_metrics, ...d.blessed_metrics }).sort();
	}

	function fmt(value: unknown): string {
		if (typeof value === 'number')
			return Number.isInteger(value) ? String(value) : value.toFixed(4);
		return value == null ? '—' : String(value);
	}

	function fmtBytes(n: number): string {
		if (n < 1024) return `${n} B`;
		if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
		if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
		return `${(n / 1024 ** 3).toFixed(1)} GiB`;
	}

	function when(ts: string | null): string {
		if (!ts) return '—';
		const d = new Date(ts);
		return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
	}

	const artifacts = $derived(detail.artifacts ?? []);
	const names = $derived(metricNames(detail));

	// ── the artifacts DataTable: sortable path/size/updated over the describe listing. ──
	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	const sortableHeader =
		(label: string) =>
		({
			column,
		}: {
			column: {
				getIsSorted(): false | 'asc' | 'desc';
				getToggleSortingHandler(): ((e: Event) => void) | undefined;
			};
		}) =>
			renderComponent(DataTableHeaderButton, {
				label,
				sorted: column.getIsSorted(),
				onclick: column.getToggleSortingHandler(),
			});

	const columns: ColumnDef<ModelArtifact>[] = [
		{
			id: 'path',
			accessorKey: 'path',
			header: sortableHeader('path'),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'size',
			accessorKey: 'size_bytes',
			header: sortableHeader('size'),
			cell: ({ row }) => renderSnippet(sizeCell, row.original),
			meta: { headerClass: 'w-28 text-right', cellClass: 'text-right' },
		},
		{
			id: 'updated',
			accessorFn: (a) => a.updated_at ?? '',
			header: sortableHeader('updated'),
			cell: ({ row }) => renderSnippet(whenCell, row.original),
			meta: { cellClass: 'whitespace-nowrap' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return artifacts;
		},
		columns,
		state: {
			get sorting() {
				return sorting;
			},
			get pagination() {
				return pagination;
			},
		},
		onSortingChange: (u) => (sorting = typeof u === 'function' ? u(sorting) : u),
		onPaginationChange: (u) => (pagination = typeof u === 'function' ? u(pagination) : u),
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});
</script>

{#snippet sizeCell(a: ModelArtifact)}
	<span class="mono num">{fmtBytes(a.size_bytes)}</span>
{/snippet}
{#snippet whenCell(a: ModelArtifact)}
	<span class="mono faint">{when(a.updated_at)}</span>
{/snippet}

<div class="detail">
	<section>
		<h4>Metrics <span class="src mono">candidate vs blessed</span></h4>
		{#if names.length === 0}
			<p class="mut">No metrics recorded for this model.</p>
		{:else}
			<table class="metrics">
				<thead>
					<tr>
						<th>metric</th>
						<th>candidate v{detail.latest_version}</th>
						<th>blessed {detail.blessed_version ? `v${detail.blessed_version}` : '—'}</th>
					</tr>
				</thead>
				<tbody>
					{#each names as name (name)}
						<tr>
							<td class="mono">{name}</td>
							<td class="mono">{fmt(detail.candidate_metrics?.[name])}</td>
							<td class="mono">{fmt(detail.blessed_metrics?.[name])}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</section>

	<section aria-label="Artifacts for {model}">
		<h4>Artifacts <span class="src mono">models/{model}/… object listing</span></h4>
		<DataTable
			{table}
			emptyMessage="No artifacts listed — the registry tree for this model holds no objects."
		/>
	</section>

	<section>
		<TrainingCurves {model} />
	</section>
</div>

<style>
	.detail {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	h4 {
		display: flex;
		align-items: baseline;
		gap: 8px;
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	.src {
		text-transform: none;
		letter-spacing: 0;
		font-size: 11px;
	}
	.metrics {
		max-width: 520px;
		border-collapse: collapse;
		font-size: 13px;
	}
	.metrics th {
		text-align: left;
		color: var(--faint);
		font-weight: 500;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		padding: 6px 10px;
		border-bottom: 1px solid var(--line);
	}
	.metrics td {
		padding: 7px 10px;
		border-bottom: 1px solid var(--line);
	}
	.mut {
		color: var(--mut);
		margin: 0;
		font-size: 12px;
	}
	.num {
		color: var(--mut);
	}
	.faint {
		color: var(--faint);
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
