<script lang="ts">
	import { resolve } from '$app/paths';
	// `/dlq` — the #83 lineage DLQ / transactional-outbox ops panel. The outbox holds events staged before
	// publish and dropped on ack; a surviving object is a committed write whose lineage was not yet confirmed
	// delivered — the at-risk set the reconcile relay drains on a timer. This panel surfaces that set and lets
	// an operator replay one on demand (re-ingest + drop) instead of waiting for the next tick. The list is
	// governed per-dataset by the service; replay forwards the signed-in user's bearer through a session-only
	// catalog door route (never the service token). Governed without a session → 401.
	import {
		createSvelteTable,
		DataTable,
		DataTableTextFilter,
		getCoreRowModel,
		getFilteredRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderSnippet,
		type ColumnDef,
		type PaginationState,
		type SortingState,
		sortableHeader,
	} from '@riksarkivet/ui/data-table';
	import { Button } from '@riksarkivet/ui/button';
	import { RefreshCw, RotateCcw, ShieldAlert, Inbox } from '@lucide/svelte';
	import { page } from '$app/state';
	import { fetchDlq, replayDlq } from '#lib/lineage/remote/lineage.api.ts';
	import type { DlqBacklog, DlqEvent } from '#lib/api/lineage/index.ts';
	import { lineageTick, liveRead } from '#lib/live/tick.svelte.ts';
	import DlqEventDrawer from './DlqEventDrawer.svelte';

	const dlqRead = fetchDlq(200);
	const result = $derived(dlqRead.current);
	// A failed read clears the view, so the auth/offline state reflects reality: a session that expires
	// after a first successful load shows the sign-in prompt, not the old event table.
	const backlog = $derived<DlqBacklog | null>(result?.ok ? result.data : null);
	const lastStatus = $derived(result === undefined ? 0 : result.ok ? 200 : result.status);
	const settled = $derived(result !== undefined);
	let busy = $state<string | null>(null); // run_id currently replaying
	let msg = $state<{ ok: boolean; text: string } | null>(null);

	const unauthorized = $derived(backlog === null && settled && lastStatus === 401);
	// 0 stays IN the offline set: after the first settle, a fetch-level failure (network down, request timeout)
	// reports status 0 and must render as offline, not hang on the loading message (audit finding).
	const offline = $derived(backlog === null && settled && ![200, 401].includes(lastStatus));

	/** Read the backlog again: `refresh()`, as a held read hands back what it already holds. */
	async function load(): Promise<void> {
		await dlqRead.refresh();
	}

	async function replay(e: DlqEvent): Promise<void> {
		if (busy) return;
		busy = e.run_id;
		msg = null;
		try {
			const res = await replayDlq(e.run_id);
			if (res.ok) {
				msg = { ok: true, text: `Replayed ${e.run_id}.` };
				await load();
			} else if (res.status === 401) {
				msg = { ok: false, text: 'Sign in to replay a lineage event.' };
			} else if (res.status === 403) {
				msg = { ok: false, text: "Denied: replay needs writer access on the event's outputs." };
			} else {
				msg = { ok: false, text: res.detail };
			}
		} finally {
			busy = null;
		}
	}

	// Live on the LINEAGE cursor, not the control one — this is the only admin panel where that is the
	// right feed. The outbox holds lineage events staged but not yet confirmed delivered, so it drains
	// exactly when the lineage feed advances: the cursor moving IS the relay making progress, and the
	// backlog shrinking is what an operator is watching for. Before this the panel read once on mount and
	// never again, so a drain in progress looked identical to a wedged one.
	liveRead(lineageTick, () => load());

	function age(seconds: number): string {
		if (seconds < 60) return `${Math.round(seconds)}s`;
		if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
		if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
		return `${Math.round(seconds / 86400)}d`;
	}

	// ── the row drawer (goal cond 8): click an at-risk event → the staged payload + replay. ──
	let drawerEvent = $state<DlqEvent | null>(null);

	function replayFromDrawer(e: DlqEvent): void {
		drawerEvent = null; // the outcome lands in the bar message; the list refreshes underneath
		void replay(e);
	}

	// ── the DataTable (goal cond 4): sortable/searchable at-risk rows, replay action preserved. ──
	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	const columns: ColumnDef<DlqEvent>[] = [
		{
			id: 'run',
			accessorKey: 'run_id',
			header: sortableHeader('run'),
			cell: ({ row }) => renderSnippet(runCell, row.original),
			meta: { cellClass: 'whitespace-nowrap' },
		},
		{
			id: 'type',
			accessorFn: (e) => (e.parseable ? (e.event_type ?? '') : 'poison'),
			header: sortableHeader('type'),
			cell: ({ row }) => renderSnippet(typeCell, row.original),
		},
		{
			id: 'job',
			accessorFn: (e) => e.job ?? '',
			header: sortableHeader('job'),
			cell: ({ row }) => renderSnippet(jobCell, row.original),
		},
		{
			id: 'outputs',
			accessorFn: (e) => (e.outputs ?? []).join(', '),
			header: 'outputs',
			cell: ({ row }) => renderSnippet(listCell, row.original.outputs ?? []),
		},
		{
			id: 'inputs',
			accessorFn: (e) => (e.inputs ?? []).join(', '),
			header: 'inputs',
			cell: ({ row }) => renderSnippet(listCell, row.original.inputs ?? []),
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { headerClass: 'w-28', cellClass: 'text-right' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return backlog?.events ?? [];
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

{#snippet runCell(e: DlqEvent)}
	<span class="mono run">{e.run_id}</span>
{/snippet}
{#snippet typeCell(e: DlqEvent)}
	<span class="mono" class:poison={!e.parseable}
		>{e.parseable ? (e.event_type ?? '—') : 'poison'}</span
	>
{/snippet}
{#snippet jobCell(e: DlqEvent)}
	<span class="mono">{e.job ?? '—'}</span>
{/snippet}
{#snippet listCell(items: string[])}
	<span class="mono">{items.join(', ') || '—'}</span>
{/snippet}
{#snippet actionsCell(e: DlqEvent)}
	{#if e.parseable}
		<Button
			variant="ghost"
			size="xs"
			disabled={busy !== null}
			aria-label="Replay {e.run_id}"
			onclick={() => replay(e)}><RotateCcw size={12} /> {busy === e.run_id ? '…' : 'Replay'}</Button
		>
	{:else}
		<span class="mut" title="An unparseable object can't be replayed; the relay drops it."
			>unreplayable</span
		>
	{/if}
{/snippet}

<div class="page">
	<header>
		<Inbox size={16} />
		<h1>DLQ</h1>
		<span class="sub mono"
			>the lineage dead letters: events waiting in the outbox, to view and replay</span
		>
	</header>

	<div class="bar">
		<Button variant="outline" size="sm" onclick={load}><RefreshCw size={13} /> Refresh</Button>
		{#if backlog}
			<span class="depth mono" class:warn={backlog.depth > 0}>
				depth {backlog.depth}{backlog.depth > 0
					? ` · oldest ${age(backlog.oldest_age_seconds)}`
					: ''}
			</span>
		{/if}
		{#if msg}<span class="msg" class:okmsg={msg.ok} class:error={!msg.ok}>{msg.text}</span>{/if}
	</div>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={15} />
			<a href={resolve(`auth/login?redirect=${encodeURIComponent(page.url.pathname)}`)}>Sign in</a> to
			view the lineage DLQ.
		</div>
	{:else if offline}
		<div class="empty"><RefreshCw size={15} /> DLQ store unreachable (HTTP {lastStatus}).</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Search at-risk events…" />
		</div>
		<DataTable
			{table}
			loading={backlog === null}
			onrowclick={(e) => (drawerEvent = e)}
			emptyMessage="The outbox is empty — every committed write's lineage has been delivered. Nothing to replay."
		/>
	{/if}
</div>

<DlqEventDrawer
	event={drawerEvent}
	busy={busy !== null}
	onclose={() => (drawerEvent = null)}
	onreplay={replayFromDrawer}
/>

<style>
	.page {
		max-width: 980px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 16px;
	}
	h1 {
		font-size: var(--text-xl);
		line-height: var(--text-xl--line-height);
		letter-spacing: var(--text-xl--letter-spacing);
		margin: 0;
	}
	.sub {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.bar {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 14px;
		flex-wrap: wrap;
	}
	.depth {
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.depth.warn {
		color: var(--warning, #d18b28);
	}
	.msg {
		font-size: 12px;
	}
	.okmsg {
		color: var(--success);
	}
	.error {
		color: var(--destructive);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--muted-foreground);
		font-size: 13px;
		padding: 30px 0;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	.run {
		color: var(--muted-foreground);
		white-space: nowrap;
	}
	.poison {
		color: var(--destructive);
	}
	.mut {
		color: var(--muted-foreground);
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
