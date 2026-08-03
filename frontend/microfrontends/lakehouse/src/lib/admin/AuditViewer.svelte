<script lang="ts">
	import { base } from '$app/paths';
	// `/audit` — the #77 admin audit-log viewer over the #41 compliance trail. Events land on the dedicated
	// `lance.audit` logger → OTLP → GreptimeDB (`opentelemetry_logs`); the /api/audit BFF queries them
	// server-side and returns parsed {timestamp, action, outcome, subject, resource}. No credential reaches
	// the browser. Governed without a session → 401; no observability stack → 501; auth-off dev → open.
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
	import * as Sheet from '@rask/ui/sheet';
	import { formatAbsolute, formatTimestamp } from '@rask/ui/utils';
	import { ExternalLink, Filter, RefreshCw, ScrollText, ShieldAlert } from '@lucide/svelte';
	import { page } from '$app/state';
	import { fetchAuditTrail } from './remote/audit.remote';
	import { controlCursor } from '$lib/live/feeds.remote';
	import { liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	type AuditEvent = {
		timestamp: string;
		action: string;
		outcome: string;
		subject: string;
		resource: string;
	};

	let events = $state<AuditEvent[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let inflight = 0;

	// Filters (applied server-side by the BFF over the returned columns). Initialized once from the
	// URL query so cross-page "related events" links (`/governance/audit?resource=…`, e.g. from the
	// tenants drawer) land pre-filtered — after that the state is the user's.
	const initial = page.url.searchParams;
	let outcome = $state(initial.get('outcome') ?? '');
	// The TIME WINDOW. Server-side, unlike the text filters: it changes the SQL, because the trail is
	// bounded to the newest 500 rows and a window applied client-side could only ever narrow what that
	// bound already threw away. Defaults to 24h — an unbounded first read of a compliance trail is
	// both slow and rarely the question being asked.
	let since = $state(initial.get('since') ?? '24h');
	let action = $state(initial.get('action') ?? '');
	let subject = $state(initial.get('subject') ?? '');
	let resource = $state(initial.get('resource') ?? '');

	const unauthorized = $derived(events === null && settled && lastStatus === 401);
	const forbidden = $derived(events === null && settled && lastStatus === 403);
	const unavailable = $derived(events === null && settled && lastStatus === 501);
	// 0 stays IN the offline set: after the first settle, a fetch-level failure (network down, BFF timeout)
	// reports status 0 and must render as offline, not hang on the loading message (audit finding).
	const offline = $derived(
		events === null && settled && ![200, 401, 403, 501].includes(lastStatus),
	);

	async function load(): Promise<void> {
		const seq = ++inflight;
		// `.refresh()` forces a server round trip — this surface re-reads on the control cursor and on
		// every filter change, so a cached previous answer is never the right one (the streams lesson).
		const params = {
			since,
			...(outcome ? { outcome } : {}),
			...(action.trim() ? { action: action.trim() } : {}),
			...(subject.trim() ? { subject: subject.trim() } : {}),
			...(resource.trim() ? { resource: resource.trim() } : {}),
		};
		const rq = fetchAuditTrail(params);
		await rq.refresh().catch(() => {});
		const res = rq.current ?? ({ ok: false, status: 0, detail: 'audit unreachable' } as const);
		if (seq !== inflight) return; // latest-wins
		settled = true;
		if (res.ok) {
			events = res.data.events;
			lastStatus = 200;
		} else {
			// Clear the stale rows on failure so the auth/forbidden/offline state reflects reality — else a
			// session that expires mid-view would keep showing the old (privileged) trail. (audit 2026-07-20)
			events = null;
			lastStatus = res.status;
		}
	}

	// Live on the CONTROL cursor. This surface used to make exactly one request in its life: it read the
	// trail on mount and then sat there, so an operator watching for a denial had to reload the page to
	// see one. Every governed action that reaches this trail is a control-plane change, so the catalog's
	// change feed is what says "there is something new to read" — and it is estate-admin gated, the same
	// bar this whole area is behind.
	//
	// It ALSO still re-reads when the outcome picker changes (tracked). The text filters stay untracked,
	// so typing does not re-fire a GreptimeDB query per keystroke — those apply on the Search button.
	liveRead(
		controlCursor,
		() => load(),
		() => `${outcome}|${since}`,
	);

	function tone(o: string): string {
		if (o === 'DENY' || o === 'FAILURE') return 'deny';
		if (o === 'ALLOW' || o === 'SUCCESS') return 'allow';
		return '';
	}
	// GreptimeDB's `timestamp` column arrives as a raw NANOSECOND epoch integer, which `new Date()`
	// cannot parse — the old local `when()` fell through to printing `1753387234123456789` at the
	// operator. The shared @rask/ui formatter reads the unit off the magnitude and returns both
	// forms: the row shows the distance and hangs the exact stamp in its tooltip.

	// ── the DataTable (goal cond 4): sortable columns + a client-side text search over the
	// server-filtered window (the Search button's filters stay the GreptimeDB-side query). ──
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

	const columns: ColumnDef<AuditEvent>[] = [
		{
			id: 'when',
			// Sort and search on the ABSOLUTE stamp, not the raw column: `YYYY-MM-DD HH:mm:ss` sorts
			// chronologically as plain text, and the window search then matches a date an operator can
			// actually type — over a nanosecond integer it matched nothing a human would enter.
			accessorFn: (e) => formatAbsolute(e.timestamp),
			header: sortableHeader('when'),
			cell: ({ row }) => renderSnippet(whenCell, row.original),
			meta: { cellClass: 'whitespace-nowrap' },
		},
		{
			id: 'action',
			accessorKey: 'action',
			header: sortableHeader('action'),
			cell: ({ row }) => renderSnippet(plainCell, row.original.action),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'outcome',
			accessorKey: 'outcome',
			header: sortableHeader('outcome'),
			cell: ({ row }) => renderSnippet(outcomeCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'subject',
			accessorKey: 'subject',
			header: sortableHeader('subject'),
			cell: ({ row }) => renderSnippet(plainCell, row.original.subject),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'resource',
			accessorKey: 'resource',
			header: sortableHeader('resource'),
			cell: ({ row }) => renderSnippet(plainCell, row.original.resource),
			meta: { cellClass: 'font-mono' },
		},
	];

	// ── the row drawer (goal cond 8): click an event → the full record + linked context. ──
	let drawerEvent = $state<AuditEvent | null>(null);

	/** Map an audit resource id to its estate page, when one exists. These target the catalog AREA of
	 *  this same zone, so they are base-relative soft navigations rather than cross-zone hard navs. */
	function resourceHref(res: string): string | null {
		if (res.startsWith('table:'))
			return `${base}/catalog/tables/${encodeURIComponent(res.slice('table:'.length))}`;
		if (res.startsWith('namespace:'))
			return `${base}/catalog/namespaces/${encodeURIComponent(res.slice('namespace:'.length))}`;
		if (res.startsWith('warehouse:')) return `${base}/catalog/warehouses`;
		return null;
	}

	/** Related events: narrow the trail to this event's subject or resource (the drawer's pivot). */
	function filterRelated(kind: 'subject' | 'resource'): void {
		const e = drawerEvent;
		if (!e) return;
		if (kind === 'subject') {
			subject = e.subject;
			resource = '';
		} else {
			resource = e.resource;
			subject = '';
		}
		drawerEvent = null;
		load();
	}

	const table = createSvelteTable({
		get data() {
			return events ?? [];
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

{#snippet whenCell(e: AuditEvent)}
	{@const t = formatTimestamp(e.timestamp)}
	<span class="mono when" title={t.title}>{t.relative}</span>
{/snippet}
{#snippet plainCell(value: string)}
	<span class="mono">{value || '—'}</span>
{/snippet}
{#snippet outcomeCell(e: AuditEvent)}
	<span class="mono {tone(e.outcome)}">{e.outcome || '—'}</span>
{/snippet}

<div class="page">
	<header>
		<ScrollText size={16} />
		<h1>Audit log</h1>
		<span class="sub mono">who / what / outcome · #41 compliance trail from GreptimeDB</span>
	</header>

	<div class="filters">
		<Select
			bind:value={since}
			ariaLabel="Time window"
			placeholder="last 24 hours"
			options={[
	{ value: '1h', label: 'last hour' },
	{ value: '24h', label: 'last 24 hours' },
	{ value: '7d', label: 'last 7 days' },
	{ value: '30d', label: 'last 30 days' },
	{ value: 'all', label: 'all time' },
]}
		/>
		<Select
			bind:value={outcome}
			ariaLabel="Outcome filter"
			placeholder="any outcome"
			options={[
	{ value: '', label: 'any outcome' },
	{ value: 'ALLOW', label: 'ALLOW' },
	{ value: 'DENY', label: 'DENY' },
	{ value: 'SUCCESS', label: 'SUCCESS' },
	{ value: 'FAILURE', label: 'FAILURE' },
]}
		/>
		<input
			class="mono"
			bind:value={action}
			placeholder="action (e.g. can_drop)"
			aria-label="Action filter"
		/>
		<input
			class="mono"
			bind:value={subject}
			placeholder="subject contains…"
			aria-label="Subject filter"
		/>
		<input
			class="mono"
			bind:value={resource}
			placeholder="resource contains…"
			aria-label="Resource filter"
		/>
		<button class="btn" onclick={load}>Search</button>
	</div>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={15} /> <a href={loginHref} data-sveltekit-reload>Sign in</a> to view the audit trail.
		</div>
	{:else if forbidden}
		<div class="empty">
			<ShieldAlert size={15} /> The audit trail is estate-admin only — it spans every tenant.
		</div>
	{:else if unavailable}
		<div class="empty">The audit viewer needs the observability stack (GreptimeDB).</div>
	{:else if offline}
		<div class="empty"><RefreshCw size={15} /> Audit store unreachable (HTTP {lastStatus}).</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Search this window…" />
		</div>
		<DataTable
			{table}
			loading={events === null}
			onrowclick={(e) => (drawerEvent = e)}
			emptyMessage="No audit events match — widen the filters, or the trail is empty for this window."
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
				<Sheet.Title>Audit event</Sheet.Title>
				<Sheet.Description>
					One record off the #41 compliance trail — who did what, and how it was decided.
				</Sheet.Description>
			</Sheet.Header>
			<div class="drawer-body">
				<dl class="record">
					<dt>when</dt>
					<!-- The drawer is the full record, so it leads with the exact stamp and carries the
					     distance beside it rather than hiding it in a tooltip. -->
					<dd class="mono">
						{formatTimestamp(drawerEvent.timestamp).absolute}
						<span class="faint">({formatTimestamp(drawerEvent.timestamp).relative})</span>
					</dd>
					<dt>action</dt>
					<dd class="mono">{drawerEvent.action || '—'}</dd>
					<dt>outcome</dt>
					<dd class="mono {tone(drawerEvent.outcome)}">{drawerEvent.outcome || '—'}</dd>
					<dt>subject</dt>
					<dd class="mono">{drawerEvent.subject || '—'}</dd>
					<dt>resource</dt>
					<dd class="mono">{drawerEvent.resource || '—'}</dd>
				</dl>
				<div class="drawer-links">
					{#if drawerEvent.subject}
						<button class="btn" onclick={() => filterRelated('subject')}>
							<Filter size={12} /> Events by this subject
						</button>
					{/if}
					{#if drawerEvent.resource}
						<button class="btn" onclick={() => filterRelated('resource')}>
							<Filter size={12} /> Events on this resource
						</button>
						{#if resourceHref(drawerEvent.resource)}
							<!-- Cross-zone jump: leaves this zone's route manifest, so hard-navigate. -->
							<a class="btn jumplink" href={resourceHref(drawerEvent.resource)} data-sveltekit-reload>
								<ExternalLink size={12} /> Open resource ↗
							</a>
						{/if}
					{/if}
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
	.filters {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-bottom: 14px;
	}
	.filters input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
		flex: 1 1 160px;
		min-width: 120px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 14px;
		cursor: pointer;
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
	.when {
		color: var(--faint);
		white-space: nowrap;
	}
	.faint {
		color: var(--faint);
	}
	.allow {
		color: var(--ok);
	}
	.deny {
		color: var(--fail);
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
	.drawer-links {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 8px;
	}
	.drawer-links .btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
	}
	.jumplink {
		text-decoration: none;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
