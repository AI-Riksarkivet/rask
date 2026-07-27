<script lang="ts">
	// `/tables` — the catalog table registry (#52) on the shared @rask/ui DataTable (goal cond 4):
	// sortable columns, a text search, and a medallion STAGE filter (goal cond 3 — the stage is
	// derived from the namespace segment, shown as a tier badge per row). Same stack-mode states as
	// before: governed without a session ⇒ sign-in, unreachable ⇒ retrying, open ⇒ data or the
	// honest empty state. The #85 "Declare table" form (the browser-shaped create) is preserved.
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
	import { Plus, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { declareTable, fetchTables } from './catalog';
	import RowDrawer from './RowDrawer.svelte';
	import { namespaceOfTable, stageOfTable, type StageInfo } from './stage';
	import StageBadge from './StageBadge.svelte';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let tables = $state<string[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false); // distinguishes "still loading" (0, unsettled) from a network error (0, settled)

	const unauthorized = $derived(tables === null && lastStatus === 401);
	const offline = $derived(tables === null && settled && lastStatus !== 401);

	// #85 declare-table form state — hidden behind a toggle so the registry stays a list by default.
	let declaring = $state(false);
	let declNs = $state('');
	let declName = $state('');
	let declLocation = $state(''); // optional — empty means the catalog picks the location
	let declBusy = $state(false);
	let declMsg = $state<{ ok: boolean; text: string } | null>(null);

	async function load(): Promise<void> {
		const res = await fetchTables();
		settled = true;
		if (res.ok) {
			// A copy + sort, not toSorted() — the latter is ES2023, above the repo's Safari-16 floor.
			tables = [...res.data.tables].sort();
			lastStatus = 200;
		} else {
			lastStatus = res.status; // status 0 (offline/timeout) now reads as offline, not a stuck spinner
		}
	}

	// The LINEAGE cursor, not the catalog control feed: the control feed is estate-admin only (a terminal
	// 403 for everyone else), while this registry is for any signed-in user — and a table appearing is a
	// lineage event (the catalog records its creator on one), so the governed-per-subject feed sees it.
	// A transient failure keeps the last-good rows and re-reads on the next advance, so "retrying" is
	// still honest.
	liveRead(lineageTick, () => load());

	async function runDeclare(): Promise<void> {
		const ns = declNs.trim();
		const name = declName.trim();
		if (declBusy || !ns || !name) return;
		declBusy = true;
		declMsg = null;
		try {
			const res = await declareTable(ns, name, declLocation.trim() || undefined);
			if (res.ok) {
				declMsg = {
					ok: true,
					text: `declared ${ns}$${name}${res.data.location ? ` @ ${res.data.location}` : ''}`,
				};
				declNs = '';
				declName = '';
				declLocation = '';
				await load(); // pull the declared table into the registry
			} else if (res.status === 401) {
				declMsg = { ok: false, text: 'Sign in to declare a table.' };
			} else if (res.status === 403) {
				declMsg = {
					ok: false,
					text: `Denied: declaring in ${ns} needs create access (can_create_table).`,
				};
			} else if (res.status === 0) {
				declMsg = { ok: false, text: 'Catalog unreachable — the declare was not applied.' };
			} else {
				declMsg = { ok: false, text: res.detail };
			}
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			declMsg = { ok: false, text: `declare response drifted from the contract: ${String(err)}` };
		} finally {
			declBusy = false;
		}
	}

	// ── the DataTable (goal cond 4) ──
	type Row = { id: string; namespace: string; stage: StageInfo | null };
	const rows = $derived.by((): Row[] => {
		const all = (tables ?? []).map(
			(id): Row => ({
				id,
				namespace: namespaceOfTable(id),
				stage: stageOfTable(id),
			}),
		);
		return stageFilter ? all.filter((r) => r.stage?.stage === stageFilter) : all;
	});

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });
	let stageFilter = $state(''); // '' = any stage

	// Goal cond 8: row click opens the record drawer (the in-cell links keep navigating — they
	// stopPropagation so a link click never also opens the drawer).
	let drawerOpen = $state(false);
	let drawerRow = $state<Row | null>(null);
	function openDrawer(row: Row): void {
		drawerRow = row;
		drawerOpen = true;
	}

	const columns: ColumnDef<Row>[] = [
		{
			id: 'table',
			accessorKey: 'id',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'table',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(tableCell, row.original),
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
			id: 'namespace',
			accessorKey: 'namespace',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'namespace',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nsCell, row.original),
			meta: { headerClass: 'w-48' },
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

{#snippet tableCell(row: Row)}
	<a
		class="rowlink mono"
		href={`${base}/data/tables/${encodeURIComponent(row.id)}`}
		onclick={(e) => e.stopPropagation()}>{row.id}</a
	>
{/snippet}
{#snippet stageCell(row: Row)}
	{#if row.stage}<StageBadge info={row.stage} />{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet nsCell(row: Row)}
	<a
		class="nslink mono"
		href={`${base}/data/namespaces/${encodeURIComponent(row.namespace)}`}
		onclick={(e) => e.stopPropagation()}>{row.namespace}</a
	>
{/snippet}

<div class="page">
	<header>
		<h1>Tables</h1>
		<span class="sub mono">the catalog registry · &lt;namespace&gt;$&lt;table&gt;</span>
		{#if !unauthorized}
			<button class="new" onclick={() => (declaring = !declaring)}>
				<Plus size={12} /> Declare table
			</button>
		{/if}
	</header>

	{#if declaring && !unauthorized}
		<!-- #85 the browser-shaped create: declare an empty table (JSON, no Arrow). -->
		<form
			class="declare"
			onsubmit={(e) => {
	e.preventDefault();
	runDeclare();
}}
		>
			<input class="mono" bind:value={declNs} placeholder="namespace" aria-label="Namespace" />
			<input class="mono" bind:value={declName} placeholder="table name" aria-label="Table name" />
			<input
				class="mono loc"
				bind:value={declLocation}
				placeholder="location (optional — catalog picks)"
				aria-label="Location"
			/>
			<button class="btn" type="submit" disabled={declBusy || !declNs.trim() || !declName.trim()}>
				{declBusy ? '…' : 'Declare'}
			</button>
		</form>
	{/if}
	{#if declMsg}
		<div class="banner" class:ok={declMsg.ok} class:fail={!declMsg.ok}>{declMsg.text}</div>
	{/if}

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to browse the catalog.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}) — retrying.</p>
		</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Search tables…" />
			<Select
				bind:value={stageFilter}
				ariaLabel="Stage filter"
				placeholder="any stage"
				options={[
	{ value: '', label: 'any stage' },
	{ value: 'raw', label: 'raw' },
	{ value: 'bronze', label: 'bronze' },
	{ value: 'silver', label: 'silver' },
	{ value: 'gold', label: 'gold' },
]}
			/>
		</div>
		<DataTable
			{table}
			loading={tables === null}
			emptyMessage="No tables registered — a create (or the medallion cascade's gold sink) makes the first."
			onrowclick={openDrawer}
		/>
	{/if}
</div>

<RowDrawer
	bind:open={drawerOpen}
	title={drawerRow?.id ?? ''}
	description="The registry record for this table."
>
	{#if drawerRow}
		<dl class="rec">
			<dt>table id</dt>
			<dd class="mono">{drawerRow.id}</dd>
			<dt>namespace</dt>
			<dd>
				<a class="mono jump" href={`${base}/data/namespaces/${encodeURIComponent(drawerRow.namespace)}`}
					>{drawerRow.namespace}</a
				>
			</dd>
			<dt>medallion stage</dt>
			<dd>
				{#if drawerRow.stage}<StageBadge info={drawerRow.stage} />{:else}<span class="mut"
						>— (not a medallion zone)</span
					>{/if}
			</dd>
		</dl>
		<div class="jumps">
			<a class="btn" href={`${base}/data/tables/${encodeURIComponent(drawerRow.id)}`}>Open detail</a>
			<!-- R18 table previewer: deep-link onto the detail pane's preview tab, which drives the
			     existing /capi query machinery (first-N rows on the shared data-table). -->
			<a class="btn" href={`${base}/data/tables/${encodeURIComponent(drawerRow.id)}?tab=preview`}
				>Preview</a
			>
			<a class="btn" href={`${base}/data/tables/${encodeURIComponent(drawerRow.id)}?tab=access`}
				>Access tab</a
			>
		</div>
	{/if}
</RowDrawer>

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
		cursor: pointer;
	}
	.new:hover {
		border-color: var(--mut);
	}
	.declare {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-bottom: 12px;
	}
	.declare input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
	}
	.declare .loc {
		flex: 1;
		min-width: 220px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 10px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
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
	.rowlink {
		color: var(--ink);
		text-decoration: none;
	}
	.rowlink:hover {
		text-decoration: underline;
	}
	.nslink {
		color: var(--mut);
		text-decoration: none;
	}
	.nslink:hover {
		color: var(--ink);
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
	.jump {
		color: var(--ink);
	}
	.jumps {
		display: flex;
		gap: 8px;
	}
	.jumps .btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		text-decoration: none;
	}
	.jumps .btn:hover {
		border-color: var(--mut);
	}
</style>
