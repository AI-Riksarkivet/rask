<script lang="ts">
	// `/namespaces` — the catalog's namespaces on the shared @rask/ui DataTable (goal cond 4):
	// one sortable/searchable row per namespace (derived from the table registry's
	// `<namespace>$<table>` ids — there is no root-namespace list endpoint), with the medallion
	// tier badge (goal cond 3) and the #85 drop action preserved (AlertDialog confirm; Restrict by
	// default, Cascade opt-in). Creation deliberately has NO surface here — the governed path is
	// the warehouse-bind flow (/warehouses), which the "New namespace" affordance points at.
	import { AlertDialog } from '@rask/ui/alert-dialog';
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
	import { Plus, RefreshCw, ShieldAlert, Trash2 } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { fetchTables } from './remote/catalog.remote';
	import { dropNamespace } from './remote/namespace.remote';
	import { fetchEstateBindings } from './remote/warehouses.remote';
	import RowDrawer from './RowDrawer.svelte';
	import { namespaceOfTable, stageOf, type StageInfo } from './stage';
	import StageBadge from './StageBadge.svelte';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let tables = $state<string[] | null>(null);
	// #83 the namespaces BOUND in the registry — the same source the drop door consults. Without it
	// this page lists only namespaces that already hold a table, so a freshly-bound EMPTY namespace is
	// invisible: exactly the object someone came here to find and drop (the #66 defect, second surface).
	//
	// `null` means the bindings READ FAILED, and it is a distinct state from "no bindings" (#86). The
	// first version of this used `string[]` and swallowed a 403/502 into an empty array — which is
	// #66's exact shape, an invisible namespace with no error, reintroduced by the fix for it. The
	// warehouse detail page has always modelled it this way; now both agree.
	let bindings = $state<Record<string, string> | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let busy = $state(false);
	let banner = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);
	let dropOpen = $state(false);
	let dropTarget = $state<string | null>(null);
	let cascade = $state(false);

	const unauthorized = $derived(tables === null && lastStatus === 401);
	const offline = $derived(tables === null && settled && lastStatus !== 401);

	async function load(): Promise<void> {
		// Independent reads, so they go together: the table registry supplies COUNTS, the estate
		// bindings supply what EXISTS. One catalog call each — no per-warehouse fan-out (#86).
		const [res, binds] = await Promise.all([fetchTables(), fetchEstateBindings()]);
		settled = true;
		if (res.ok) {
			tables = [...res.data.tables].sort();
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
		// Keep the whole MAP, not just its keys. `fetchEstateBindings` answers
		// `{bindings: Record<namespace, warehouse_id>}` and this line used to take `Object.keys(...)`,
		// throwing every value away — so the one read that knows which warehouse holds a namespace
		// discarded it, and no surface in the zone could answer that question bottom-up. The bind form on
		// `/catalog/warehouses` calls the binding IMMUTABLE ONCE SET, so it was a permanent fact you
		// could create and then see from neither end. Same request, same cost.
		bindings = binds.ok ? binds.data.bindings : null;
	}

	// Same source as the table registry it groups (this view IS the table list, folded by namespace), so
	// the two can no longer disagree about what exists — they now advance on one shared cursor.
	liveRead(lineageTick, () => load());

	// Group by the namespace segment (before the first `$`); a bare name with no delimiter is its own root.
	type Row = { ns: string; count: number; stage: StageInfo | null; warehouse: string | null };
	const rows = $derived.by((): Row[] => {
		const m = new Map<string, number>();
		// Seed with every BOUND namespace at zero, so one holding no tables still gets a row. `null`
		// (the read failed) seeds nothing and the banner below says so — never silently "none".
		for (const ns of Object.keys(bindings ?? {})) m.set(ns, 0);
		for (const t of tables ?? []) {
			const ns = namespaceOfTable(t);
			m.set(ns, (m.get(ns) ?? 0) + 1);
		}
		return [...m.entries()]
			// A namespace with no binding shows `—`, not a blank: unbound is a real state (the resolver
			// falls back to the default root) and rendering it as absence hides it.
			.map(([ns, count]): Row => ({ ns, count, stage: stageOf(ns), warehouse: bindings?.[ns] ?? null }))
			.sort((a, b) => a.ns.localeCompare(b.ns));
	});

	// Tables inside the namespace queued for drop — sizes the Cascade choice honestly.
	const targetCount = $derived(
		dropTarget === null ? 0 : (rows.find((r) => r.ns === dropTarget)?.count ?? 0),
	);

	function openDrop(ns: string): void {
		dropTarget = ns;
		cascade = false;
		banner = null;
		dropOpen = true;
	}

	function fail(ns: string, status: number, detail: string): void {
		if (status === 401)
			banner = { tone: 'fail', text: 'Sign in — dropping a namespace is a per-user action.' };
		else if (status === 403)
			banner = { tone: 'fail', text: `Denied: dropping ${ns} needs the owner rung (can_delete).` };
		else if (status === 0)
			banner = { tone: 'fail', text: 'Catalog unreachable — the drop was not applied.' };
		else banner = { tone: 'fail', text: detail };
	}

	async function confirmDrop(): Promise<void> {
		const ns = dropTarget;
		if (ns === null || busy) return;
		busy = true;
		banner = null;
		try {
			const res = await dropNamespace({ namespace: ns, cascade });
			if (res.ok) {
				banner = { tone: 'ok', text: `namespace ${ns} dropped${cascade ? ' (cascade)' : ''}` };
				await load();
			} else {
				fail(ns, res.status, res.detail);
			}
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			banner = { tone: 'fail', text: `drop response drifted from the contract: ${String(err)}` };
		} finally {
			// ALWAYS close + disarm: bits-ui's AlertDialog.Action does not auto-close, so leaving the dialog
			// open would keep the destructive action armed for a second, confirm-free fire (audit: major).
			// The banner carries success/failure either way.
			busy = false;
			dropOpen = false;
			dropTarget = null;
		}
	}

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	// Goal cond 8: row click opens the record drawer (links/buttons in cells stopPropagation).
	let drawerOpen = $state(false);
	let drawerRow = $state<Row | null>(null);
	function openDrawer(row: Row): void {
		drawerRow = row;
		drawerOpen = true;
	}

	const columns: ColumnDef<Row>[] = [
		{
			id: 'namespace',
			accessorKey: 'ns',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'namespace',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nsCell, row.original),
		},
		{
			id: 'stage',
			accessorFn: (r) => r.stage?.stage ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'stage',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(stageCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			// WHICH WAREHOUSE HOLDS THIS. The binding arrives in the same `fetchEstateBindings` read that
			// seeds the empty-namespace rows, so this column costs nothing extra — it was simply thrown
			// away. Bottom-up was unanswerable in this zone until now: the bind form lives on the
			// warehouses page and its caption calls the binding immutable once set.
			id: 'warehouse',
			accessorFn: (r) => r.warehouse ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'warehouse',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(warehouseCell, row.original),
			meta: { headerClass: 'w-40' },
		},
		{
			id: 'tables',
			accessorKey: 'count',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'tables',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			meta: { headerClass: 'w-24', cellClass: 'tabular-nums' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { headerClass: 'w-20', cellClass: 'text-right' },
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

{#snippet nsCell(row: Row)}
	<a
		class="ns-name mono"
		href={`${base}/catalog/namespaces/${encodeURIComponent(row.ns)}`}
		onclick={(e) => e.stopPropagation()}>{row.ns}</a
	>
{/snippet}
{#snippet stageCell(row: Row)}
	{#if row.stage}<StageBadge info={row.stage} />{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet warehouseCell(row: Row)}
	<!-- A link, because the warehouse has a detail page and the whole point is that this rung was
	     unreachable from here. `—` for unbound, which is a real state (the resolver falls back to the
	     default root), not missing data. -->
	{#if row.warehouse}
		<a class="wh" href={`${base}/catalog/warehouses/${row.warehouse}`} onclick={(e) => e.stopPropagation()}>
			{row.warehouse}
		</a>
	{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet actionsCell(row: Row)}
	<button
		class="drop"
		aria-label={`Drop namespace ${row.ns}`}
		disabled={busy}
		onclick={(e) => {
	e.stopPropagation();
	openDrop(row.ns);
}}
	>
		<Trash2 size={12} /> drop
	</button>
{/snippet}

<div class="page">
	<header>
		<h1>Namespaces</h1>
		<span class="sub mono">grouped from the catalog registry, estate-wide — every namespace your grants allow · &lt;namespace&gt;$&lt;table&gt;</span>
		<a
			class="new"
			href={`${base}/catalog/warehouses`}
			title="Namespaces are created through the governed warehouse-bind flow"
		>
			<Plus size={12} /> New namespace
		</a>
	</header>

	<!-- #86: a failed bindings read is its own state. Collapsing it into "no namespaces" is the #66
	     defect — an empty namespace invisible with nothing said — and this page reintroduced it once. -->
	{#if bindings === null && settled && !unauthorized && !offline}
		<!-- Its OWN class, not `.banner.fail`: that one means "the action you just took failed", this
		     means "a read this page depends on is unavailable". Sharing a selector made a spec's strict
		     locator match two elements the moment both could appear. -->
		<div class="banner degraded" data-testid="bindings-unavailable">
			Namespace bindings unavailable — this list shows only namespaces that already hold a table, so a
			bound-but-empty one may be missing.
		</div>
	{/if}
	{#if banner}
		<div class="banner" class:ok={banner.tone === 'ok'} class:fail={banner.tone === 'fail'}>
			{banner.text}
		</div>
	{/if}

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to browse namespaces.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}) — retrying.</p>
		</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Search namespaces…" />
		</div>
		<DataTable
			{table}
			loading={tables === null}
			emptyMessage="No namespaces yet — bind one to a warehouse to create the first."
			onrowclick={openDrawer}
		/>
	{/if}
</div>

<RowDrawer
	bind:open={drawerOpen}
	title={drawerRow?.ns ?? ''}
	description="The registry record for this namespace (grouped from the table registry)."
>
	{#if drawerRow}
		<dl class="rec">
			<dt>namespace</dt>
			<dd class="mono">{drawerRow.ns}</dd>
			<dt>medallion stage</dt>
			<dd>
				{#if drawerRow.stage}<StageBadge info={drawerRow.stage} />{:else}<span class="mut"
						>— (not a medallion zone)</span
					>{/if}
			</dd>
			<dt>tables</dt>
			<dd class="mono">{drawerRow.count}</dd>
		</dl>
		<div class="jumps">
			<a class="jbtn" href={`${base}/catalog/namespaces/${encodeURIComponent(drawerRow.ns)}`}
				>Open detail</a
			>
		</div>
	{/if}
</RowDrawer>

<AlertDialog.Root bind:open={dropOpen}>
	<AlertDialog.Content>
		<AlertDialog.Title>Drop namespace {dropTarget}</AlertDialog.Title>
		<AlertDialog.Description>
			This permanently drops <span class="mono">{dropTarget}</span> from the catalog (owner-gated: can_delete).
			The default Restrict behavior refuses a non-empty namespace — tick Cascade to also drop everything
			inside it.
		</AlertDialog.Description>
		<label class="cascade">
			<input type="checkbox" bind:checked={cascade} disabled={busy} />
			Cascade — also drop the {targetCount} table{targetCount === 1 ? '' : 's'} inside
		</label>
		<div class="dialog-actions">
			<AlertDialog.Cancel disabled={busy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="border-destructive/40 bg-destructive/15 text-destructive hover:bg-destructive/25"
				disabled={busy}
				onclick={confirmDrop}
			>
				Drop
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
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
	.new {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		margin-left: auto;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		text-decoration: none;
	}
	.new:hover {
		border-color: var(--mut);
	}
	.banner {
		padding: 8px 12px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--line);
		margin-bottom: 12px;
		font-size: 13px;
	}
	.banner.ok {
		border-color: color-mix(in srgb, var(--ok) 45%, var(--line));
		color: var(--ok);
	}
	.banner.degraded {
		border-color: color-mix(in srgb, var(--warn) 50%, var(--line));
		background: color-mix(in srgb, var(--warn) 7%, transparent);
	}
	.banner.fail {
		border-color: color-mix(in srgb, var(--fail) 45%, var(--line));
		color: var(--fail);
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	.ns-name {
		color: var(--ink);
		text-decoration: none;
	}
	.ns-name:hover {
		text-decoration: underline;
	}
	.wh {
		color: var(--color-foreground);
		text-decoration: none;
	}
	.wh:hover {
		text-decoration: underline;
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
	.drop {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		background: none;
		border: none;
		border-radius: var(--radius-sm);
		color: var(--faint);
		font-size: 11px;
		padding: 2px 6px;
		cursor: pointer;
	}
	.drop:hover {
		color: var(--fail);
		background: var(--panel-2);
	}
	.drop:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.cascade {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		font-size: 13px;
	}
	/* drawer record + jump links */
	.rec {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 6px 14px;
		margin: 0;
		font-size: 12px;
	}
	.rec dt {
		color: var(--faint);
	}
	.rec dd {
		margin: 0;
		color: var(--ink);
		word-break: break-all;
	}
	.jumps {
		display: flex;
		gap: 8px;
	}
	.jbtn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		text-decoration: none;
	}
	.jbtn:hover {
		border-color: var(--mut);
	}
	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
</style>
