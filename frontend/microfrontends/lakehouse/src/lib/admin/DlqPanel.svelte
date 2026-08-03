<script lang="ts">
	import { base } from '$app/paths';
	// `/dlq` — the #83 lineage DLQ / transactional-outbox ops panel. The outbox holds events staged before
	// publish and dropped on ack; a surviving object is a committed write whose lineage was not yet confirmed
	// delivered — the at-risk set the reconcile relay drains on a timer. This panel surfaces that set and lets
	// an operator replay one on demand (re-ingest + drop) instead of waiting for the next tick. The list is
	// governed per-dataset by the service; replay forwards the signed-in user's bearer through a session-only
	// BFF route (never the service token). Governed without a session → 401.
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
	import * as Sheet from '@rask/ui/sheet';
	import { ExternalLink, RefreshCw, RotateCcw, ShieldAlert, Inbox } from '@lucide/svelte';
	import { page } from '$app/state';
	import { fetchDlq, replayDlq } from '$lib/api';
	import type { DlqBacklog, DlqEvent } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let backlog = $state<DlqBacklog | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let inflight = 0;
	let busy = $state<string | null>(null); // run_id currently replaying
	let msg = $state<{ ok: boolean; text: string } | null>(null);

	const unauthorized = $derived(backlog === null && settled && lastStatus === 401);
	// 0 stays IN the offline set: after the first settle, a fetch-level failure (network down, BFF timeout)
	// reports status 0 and must render as offline, not hang on the loading message (audit finding).
	const offline = $derived(backlog === null && settled && ![200, 401].includes(lastStatus));

	async function load(): Promise<void> {
		const seq = ++inflight;
		const res = await fetchDlq(200);
		if (seq !== inflight) return; // latest-wins
		settled = true;
		if (res.ok) {
			backlog = res.data;
			lastStatus = 200;
		} else {
			// Clear the stale view on failure so the auth/offline state reflects reality — else a session that
			// expires after a first successful load would keep showing the old event table (the derivations
			// gate on backlog === null) instead of the sign-in prompt. (audit 2026-07-20)
			backlog = null;
			lastStatus = res.status;
		}
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

	async function replayFromDrawer(): Promise<void> {
		const e = drawerEvent;
		if (!e) return;
		drawerEvent = null; // the outcome lands in the bar message; the list refreshes underneath
		await replay(e);
	}

	// ── the DataTable (goal cond 4): sortable/searchable at-risk rows, replay action preserved. ──
	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
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
		<button
			class="btn ghost"
			disabled={busy !== null}
			aria-label="Replay {e.run_id}"
			onclick={(ev) => {
	// The row itself opens the drawer — the inline replay must not also do that.
	ev.stopPropagation();
	replay(e);
}}><RotateCcw size={12} /> {busy === e.run_id ? '…' : 'Replay'}</button
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
		<h1>Lineage DLQ</h1>
		<span class="sub mono">transactional-outbox at-risk events · view + replay (#83)</span>
	</header>

	<div class="bar">
		<button class="btn" onclick={load}><RefreshCw size={13} /> Refresh</button>
		{#if backlog}
			<span class="depth mono" class:warn={backlog.depth > 0}>
				depth {backlog.depth}{backlog.depth > 0 ? ` · oldest ${age(backlog.oldest_age_seconds)}` : ''}
			</span>
		{/if}
		{#if msg}<span class="msg" class:okmsg={msg.ok} class:error={!msg.ok}>{msg.text}</span>{/if}
	</div>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={15} /> <a href={loginHref} data-sveltekit-reload>Sign in</a> to view the lineage
			DLQ.
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

<Sheet.Root
	open={drawerEvent !== null}
	onOpenChange={(o) => {
	if (!o) drawerEvent = null;
}}
>
	<Sheet.Content side="right">
		{#if drawerEvent}
			<Sheet.Header>
				<Sheet.Title>Staged event {drawerEvent.run_id}</Sheet.Title>
				<Sheet.Description>
					{drawerEvent.parseable
						? 'A committed write whose lineage is not yet confirmed delivered — replay re-ingests it now instead of waiting for the relay tick.'
						: 'A poison object: unparseable, so it cannot be replayed — the relay drops it on its next pass.'}
				</Sheet.Description>
			</Sheet.Header>
			<div class="drawer-body">
				<dl class="record">
					<dt>run</dt>
					<dd class="mono">{drawerEvent.run_id}</dd>
					<dt>type</dt>
					<dd class="mono" class:poison={!drawerEvent.parseable}>
						{drawerEvent.parseable ? (drawerEvent.event_type ?? '—') : 'poison'}
					</dd>
					<dt>event time</dt>
					<dd class="mono">{drawerEvent.event_time ?? '—'}</dd>
					<dt>job</dt>
					<dd class="mono">{drawerEvent.job ?? '—'}</dd>
					<dt>inputs</dt>
					<dd class="mono">{(drawerEvent.inputs ?? []).join(', ') || '—'}</dd>
					<dt>outputs</dt>
					<dd class="mono">{(drawerEvent.outputs ?? []).join(', ') || '—'}</dd>
				</dl>
				<div>
					<h4 class="drawer-h">Payload (parsed staged fields)</h4>
					<pre
       class="payload"
       aria-label="Staged event payload"><code>{JSON.stringify(drawerEvent, null, 2)}</code></pre>
				</div>
				<div class="drawer-links">
					{#if drawerEvent.parseable}
						<button class="btn" disabled={busy !== null} onclick={replayFromDrawer}>
							<RotateCcw size={12} /> Replay this event
						</button>
					{/if}
					{#each drawerEvent.outputs ?? [] as out (out)}
						<!-- The output dataset's table page is in the catalog AREA of this zone — soft nav. -->
						<a class="btn jumplink" href={`${base}/catalog/tables/${encodeURIComponent(out)}`}>
							<ExternalLink size={12} />
							{out} ↗
						</a>
					{/each}
				</div>
			</div>
		{/if}
	</Sheet.Content>
</Sheet.Root>

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
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.bar {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 14px;
		flex-wrap: wrap;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.btn.ghost {
		padding: 2px 8px;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.depth {
		font-size: 12px;
		color: var(--mut);
	}
	.depth.warn {
		color: var(--warn, #d18b28);
	}
	.msg {
		font-size: 12px;
	}
	.okmsg {
		color: var(--ok);
	}
	.error {
		color: var(--fail);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
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
		color: var(--faint);
		white-space: nowrap;
	}
	.poison {
		color: var(--fail);
	}
	.mut {
		color: var(--faint);
	}
	.drawer-body {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 0 16px 16px;
		overflow-y: auto;
	}
	.record {
		display: grid;
		grid-template-columns: 84px 1fr;
		gap: 6px 10px;
		margin: 0;
		font-size: 12px;
	}
	.record dt {
		color: var(--faint);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		font-size: 10.5px;
	}
	.record dd {
		margin: 0;
		color: var(--ink);
		word-break: break-all;
	}
	.drawer-h {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 6px;
	}
	.payload {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 11px;
		line-height: 1.5;
		padding: 10px 12px;
		margin: 0;
		overflow-x: auto;
		font-family: ui-monospace, monospace;
	}
	.drawer-links {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 8px;
	}
	.jumplink {
		text-decoration: none;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
