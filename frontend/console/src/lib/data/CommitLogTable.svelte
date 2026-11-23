<script lang="ts">
	// The commit log's table (#113): the design system's DataTable over tanstack, one row per version,
	// with sorting and a real page size, which are this view's own state. It renders the rows it is
	// handed (already filtered) and reports a row's selection up; the log owns which commit is selected.
	import { ChevronDown, ChevronRight } from '@lucide/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
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
	} from '@riksarkivet/ui/data-table';
	import { fmtEpoch, opTone, type LogRow } from './history';

	let {
		rows,
		empty,
		loading,
		historyRead,
		currentVersion,
		selectedVersion,
		onselect,
		summary,
	}: {
		/** The rows to show, after the log's filters. */
		rows: LogRow[];
		/** Whether the table has no version at all (rather than none matching the filters). */
		empty: boolean;
		loading: boolean;
		/** Whether the commit log answered; without it every row is a manifest only. */
		historyRead: boolean;
		currentVersion: number | null;
		selectedVersion: number | null;
		onselect: (version: number) => void;
		/** The footer line: how many commits are shown of how many read. */
		summary: string;
	} = $props();

	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });

	const columns: ColumnDef<LogRow>[] = [
		{
			id: 'version',
			accessorKey: 'version',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'version',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(versionCell, row.original),
			meta: { headerClass: 'w-20' },
		},
		{
			id: 'operation',
			accessorFn: (r) => r.operation ?? '',
			header: 'operation',
			cell: ({ row }) => renderSnippet(operationCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'change',
			header: 'what changed',
			cell: ({ row }) => renderSnippet(changeCell, row.original),
		},
		{
			id: 'when',
			accessorFn: (r) => r.committed ?? 0,
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'when',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(whenCell, row.original),
			meta: { headerClass: 'w-36' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { headerClass: 'w-16' },
		},
	];

	const logTable = createSvelteTable({
		get data() {
			return rows;
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

	const TONE_CLASS: Record<string, string> = {
		append: 'border-transparent bg-success text-success-foreground',
		delete: 'border-transparent bg-destructive text-white',
		rewrite: 'border-transparent bg-warning text-warning-foreground',
		schema: 'border-primary/40 bg-primary/10 text-primary',
		index: 'border-teal-500/40 bg-teal-500/15 text-teal-800 dark:text-teal-200',
		restore: 'border-violet-500/40 bg-violet-500/15 text-violet-800 dark:text-violet-200',
		unknown: 'border-border bg-muted text-muted-foreground',
	};
</script>

{#snippet versionCell(row: LogRow)}
	<span class="vcell">
		<span class="mono v">v{row.version}</span>
		{#if row.version === currentVersion}<span class="tip" title="the table's current version"
				>tip</span
			>{/if}
		{#each row.refs as name (name)}
			<span class="tagchip mono" title={`tag ${name} pins v${row.version}`}>{name}</span>
		{/each}
		{#if row.schemaChange}
			<span class="schemadot" title="this commit changed the schema" aria-label="schema change"
				>◆</span
			>
		{/if}
	</span>
{/snippet}

{#snippet operationCell(row: LogRow)}
	{#if row.operation}
		<Badge variant="outline" class="px-1.5 text-[10px] {TONE_CLASS[opTone(row.operation)]}"
			>{row.operation}</Badge
		>
	{:else}
		<span
			class="mut"
			title={!historyRead
				? 'the commit log is unavailable — this row is the manifest only'
				: 'the transaction file for this version could not be read'}>—</span
		>
	{/if}
{/snippet}

{#snippet changeCell(row: LogRow)}
	{#if row.change.length > 0}
		<span class="bits" title={row.change.map((b) => b.text).join(' · ')}>
			{#each row.change as bit, i (i)}
				{#if i > 0}<span class="sep">·</span>{/if}
				<span class:mono={bit.mono} title={bit.mono ? bit.text : undefined}>{bit.text}</span>
			{/each}
		</span>
	{:else if row.operation === 'Restore'}
		<span class="mut" title="the catalog does not report the restored version yet"
			>target version not recorded</span
		>
	{:else}
		<span class="mut">—</span>
	{/if}
{/snippet}

{#snippet whenCell(row: LogRow)}
	{#if row.committed != null}
		<span class="mono when">{fmtEpoch(row.committed, 'ms')}</span>
	{:else if row.naive}
		<!-- The wire's own string, verbatim: it carries no offset, so this view will not pretend to know
		     which page it is in by reformatting it. -->
		<span
			class="mono naive when"
			title="the catalog reports this commit time without a timezone offset">{row.naive}</span
		>
	{:else}
		<span class="mut">—</span>
	{/if}
{/snippet}

{#snippet actionsCell(row: LogRow)}
	<!-- A chevron, not the word "details": five columns of real content already fill the card, and the
	     word cost more width than the whole actions column is worth. The accessible name still says what it
	     opens, and the row itself is clickable. -->
	{@const Chevron = selectedVersion === row.version ? ChevronDown : ChevronRight}
	<Button
		variant="ghost"
		size="icon-xs"
		aria-label="details for v{row.version}"
		aria-expanded={selectedVersion === row.version}
		onclick={() => onselect(row.version)}
	>
		<Chevron size={13} />
	</Button>
{/snippet}

<div data-testid="commit-log">
	<DataTable
		table={logTable}
		{loading}
		emptyMessage={empty
			? 'No versions — this table has no manifest the catalog can read.'
			: 'No commits match this filter.'}
		onrowclick={(row) => onselect(row.version)}
		rowLabel={(row) => `Show version ${row.version}`}
	>
		{#snippet footer()}
			<span class="mut">{summary}</span>
		{/snippet}
	</DataTable>
</div>

<style>
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.vcell {
		align-items: center;
		display: inline-flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.v {
		font-weight: 600;
	}
	.tip,
	.tagchip {
		border: 1px solid var(--border);
		border-radius: 999px;
		font-size: 10px;
		padding: 0 5px;
	}
	.tip {
		background: color-mix(in oklab, var(--success) 15%, transparent);
		border-color: color-mix(in oklab, var(--success) 45%, transparent);
	}
	.tagchip {
		background: color-mix(in oklab, var(--warning) 15%, transparent);
		border-color: color-mix(in oklab, var(--warning) 45%, transparent);
	}
	.schemadot {
		color: var(--warning);
		font-size: 10px;
	}
	/* The log stays scannable only if no single cell can stretch the table: a long predicate and a long
	   index name are unbroken strings. Each is capped and ellipsised, with the full text in the cell's
	   title (and, uncut, in the commit panel below). */
	.bits {
		font-size: 12px;
		display: inline-block;
		max-width: 20ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		vertical-align: bottom;
	}
	.sep {
		color: var(--muted-foreground);
		padding: 0 3px;
	}
	.naive {
		border-bottom: 1px dotted var(--muted-foreground);
	}
	.when {
		font-size: 11px;
	}
	.mono {
		font-family: var(--font-mono, ui-monospace, monospace);
	}
</style>
