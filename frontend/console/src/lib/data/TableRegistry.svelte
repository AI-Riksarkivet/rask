<script lang="ts">
	// `/tables` — the catalog table registry (#52) on the shared @rask/ui DataTable (goal cond 4):
	// sortable columns, a text search, and a medallion STAGE filter (goal cond 3 — the stage is
	// derived from the namespace segment, shown as a tier badge per row). Same stack-mode states as
	// before: governed without a session ⇒ sign-in, unreachable ⇒ retrying, open ⇒ data or the
	// honest empty state.
	//
	// THE #85 "Declare table" FORM NOW HAS ITS SECOND HALF. Declaring reserves an id and writes no
	// bytes, and nothing in the app could put the first rows in: the append door (`insertRows`)
	// opens the table's dataset to coerce the batch, so it 404s for a table that has none yet. The
	// row field below sends the same submit through the catalog's create door instead, which lands
	// the first data version into a declared-only table's already-reserved location — so declare and
	// fill are two visits to one form rather than a dead end. This is the ONLY surface where that can
	// live: a declared-only table has no dataset to describe, so its detail page is not reachable.
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
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { createTableWithRows } from './catalog';
	import { parseJsonRows, rowsToArrow } from './json-rows';
	import { missingFieldsHint } from '#lib/form-hint.ts';
	import { declareTable, fetchTables } from './remote/catalog.api';
	import ProjectFilter from './ProjectFilter.svelte';
	import ProjectFilterNotice from './ProjectFilterNotice.svelte';
	import { projectOfTable } from './project-filter.ts';
	import { ProjectIndexRead } from './project-index.svelte.ts';
	import RowDrawer from './RowDrawer.svelte';
	import { namespaceOfTable, stageOfTable, type StageInfo } from './stage';
	import StageBadge from './StageBadge.svelte';
	import { lineageTick, liveRead } from '#lib/live/tick.svelte.ts';

	// Seeded from the shared read instance: a registry mounted again (the workbench remounts a panel
	// whenever its tab is shown) paints the last rows read at once, and `load` refreshes them.
	const lastRead = fetchTables().current;
	let tables = $state<string[] | null>(lastRead?.ok ? [...lastRead.data.tables].sort() : null);
	let lastStatus = $state(lastRead?.ok ? 200 : 0);
	let settled = $state(lastRead !== undefined); // distinguishes "still loading" (0, unsettled) from a network error (0, settled)

	/** The project the rows are filtered to (`''`: every project), held by whoever mounts the
	 *  registry: the tables page keeps it in its URL, a workbench panel in its own state. */
	let {
		project: projectFilter,
		onprojectchange,
	}: { project: string; onprojectchange: (project: string) => void } = $props();

	// Each row's project, from the warehouses and the bindings; the filter narrows the rows this caller
	// was given, a view and never a boundary.
	const projects = new ProjectIndexRead();
	const filterWaits = $derived(projectFilter !== '' && projects.state.kind === 'loading');
	const filterFailure = $derived(
		projectFilter !== '' && projects.state.kind === 'failed' ? projects.state.reason : null,
	);

	const unauthorized = $derived(tables === null && lastStatus === 401);
	const offline = $derived(tables === null && settled && lastStatus !== 401);

	// What a BARE declare leaves behind, said where the user decides to make one — the dead end was
	// not only missing a door, it was silent about being a dead end.
	const DECLARE_ONLY_NOTE =
		'Left empty, this reserves the name and writes no data: the table has no version and no schema until its first write, and appending rows to it is refused. Its page, or this form with the same namespace and name, lands that first version.';

	// #85 declare-table form state — hidden behind a toggle so the registry stays a list by default.
	let declaring = $state(false);
	let declNs = $state('');
	let declName = $state('');
	let declLocation = $state(''); // optional — empty means the catalog picks the location
	let declRows = $state(''); // optional — a JSON array of row objects turns this into a create
	let declBusy = $state(false);
	let declMsg = $state<{ ok: boolean; text: string } | null>(null);
	const hasRows = $derived(declRows.trim().length > 0);
	const declareHint = $derived(
		missingFieldsHint(hasRows ? 'Creating' : 'Declaring', [
			['a namespace', declNs],
			['a table name', declName],
		]),
	);

	async function load(): Promise<void> {
		const [res] = await Promise.all([fetchTables(), projects.load()]);
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

	/** The refusal wording both submit paths share — one denial, worded once. */
	function declFail(verb: string, ns: string, status: number, detail: string): void {
		if (status === 401) declMsg = { ok: false, text: `Sign in to ${verb} a table.` };
		else if (status === 403)
			declMsg = {
				ok: false,
				text: `Denied: ${verb === 'create' ? 'creating' : 'declaring'} in ${ns} needs create access (can_create_table).`,
			};
		else if (status === 0)
			declMsg = { ok: false, text: `Catalog unreachable — the ${verb} was not applied.` };
		else declMsg = { ok: false, text: detail };
	}

	function clearDeclareForm(): void {
		declNs = '';
		declName = '';
		declLocation = '';
		declRows = '';
	}

	async function runDeclare(): Promise<void> {
		const ns = declNs.trim();
		const name = declName.trim();
		if (declBusy || !ns || !name) return;
		declBusy = true;
		declMsg = null;
		try {
			const res = await declareTable({
				namespace: ns,
				name,
				location: declLocation.trim() || undefined,
			});
			if (res.ok) {
				declMsg = {
					ok: true,
					// The reservation is real but EMPTY, and saying only "declared" is what sent people to a
					// 404 detail page next. State what exists and what does not.
					text: `declared ${ns}$${name}${res.data.location ? ` @ ${res.data.location}` : ''} — no data yet. ${DECLARE_ONLY_NOTE}`,
				};
				clearDeclareForm();
				await load(); // pull the declared table into the registry
			} else declFail('declare', ns, res.status, res.detail);
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			declMsg = { ok: false, text: `declare response drifted from the contract: ${String(err)}` };
		} finally {
			declBusy = false;
		}
	}

	/** The first write: create the table FROM the typed rows. Serves a brand-new id and a previously
	 *  declared one identically — the catalog lands the first data version into whichever location the
	 *  id already holds, which is why `location` is not sent here and is ignored when rows are given. */
	async function runCreateWithRows(): Promise<void> {
		const ns = declNs.trim();
		const name = declName.trim();
		if (declBusy || !ns || !name) return;
		const parsed = parseJsonRows(declRows);
		if (!parsed.ok) {
			declMsg = { ok: false, text: `Invalid rows: ${parsed.reason}` };
			return;
		}
		const rows = parsed.rows;
		declBusy = true;
		declMsg = null;
		try {
			// Browser-side Arrow-IPC encode: the inferred schema BECOMES the table's schema on a create,
			// so whole numbers are written as integers (`rowsToArrow`).
			const arrow = rowsToArrow(rows);
			const res = await createTableWithRows({ table: `${ns}$${name}`, arrow });
			if (res.ok) {
				declMsg = {
					ok: true,
					text: `Created ${ns}$${name} with ${rows.length} row${rows.length === 1 ? '' : 's'}.`,
				};
				clearDeclareForm();
				// EXPLICIT refresh, unlike the declare path. `declareTable` is a remote command and
				// single-flights `fetchTables().refresh()` in the browser; this write is a keep-bytes
				// catalog door route, so nothing invalidates the query cache for it — `load()` alone would re-read
				// the CACHED list and the new table would not appear until something else advanced the
				// live cursor. (Caught by the e2e, which was flaky exactly as often as the cursor ticked.)
				await fetchTables().refresh();
				await load();
			} else declFail('create', ns, res.status, res.detail);
		} catch (e) {
			declMsg = { ok: false, text: `Encode failed: ${e instanceof Error ? e.message : String(e)}` };
		} finally {
			declBusy = false;
		}
	}

	function submitDeclareForm(): void {
		if (hasRows) void runCreateWithRows();
		else void runDeclare();
	}

	// ── the DataTable (goal cond 4) ──
	type Row = { id: string; namespace: string; stage: StageInfo | null; project: string | null };
	const rows = $derived.by((): Row[] => {
		const all = (tables ?? []).map((id): Row => ({
			id,
			namespace: namespaceOfTable(id),
			stage: stageOfTable(id),
			project: projects.index ? projectOfTable(projects.index, id) : null,
		}));
		return all.filter(
			(r) =>
				(!stageFilter || r.stage?.stage === stageFilter) &&
				(!projectFilter || r.project === projectFilter),
		);
	});

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });
	let stageFilter = $state(''); // '' = any stage

	// Goal cond 8: a row click opens the record drawer; a click on a link in the row is the link's
	// (the data table leaves a click on a control to the control).
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
			id: 'project',
			accessorFn: (r) => r.project ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'project',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => row.original.project ?? '—',
			meta: { headerClass: 'w-32', cellClass: 'font-mono' },
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
		href={resolve('/catalog/tables/[table]', { table: encodeURIComponent(row.id) })}>{row.id}</a
	>
{/snippet}
{#snippet stageCell(row: Row)}
	{#if row.stage}<StageBadge info={row.stage} />{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet nsCell(row: Row)}
	<a
		class="nslink mono"
		href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(row.namespace) })}
		>{row.namespace}</a
	>
{/snippet}

<div class="page">
	<header>
		<h1>Tables</h1>
		<span class="sub mono"
			>the catalog registry, estate-wide — every table your grants allow ·
			&lt;namespace&gt;$&lt;table&gt;</span
		>
		{#if !unauthorized}
			<button class="new" onclick={() => (declaring = !declaring)}>
				<Plus size={12} /> Declare table
			</button>
		{/if}
	</header>

	{#if declaring && !unauthorized}
		<!-- #85 the browser-shaped create. Empty rows → declare (JSON, no Arrow): the id is reserved and
		     nothing is written. Rows → create: the browser encodes them to Arrow IPC and the catalog
		     lands the table's first data version, whether or not the id was declared earlier. -->
		<form
			class="declare"
			onsubmit={(e) => {
				e.preventDefault();
				submitDeclareForm();
			}}
		>
			<div class="row">
				<input class="mono" bind:value={declNs} placeholder="namespace" aria-label="Namespace" />
				<input
					class="mono"
					bind:value={declName}
					placeholder="table name"
					aria-label="Table name"
				/>
				<input
					class="mono loc"
					bind:value={declLocation}
					placeholder="location (optional — catalog picks)"
					aria-label="Location"
					disabled={hasRows}
					title={hasRows
						? 'Ignored when rows are given: the create lands into the location the id already holds.'
						: undefined}
				/>
			</div>
			<textarea
				class="mono rows"
				bind:value={declRows}
				placeholder={'rows (optional) — [{ "id": 1, "name": "a" }]'}
				aria-label="Initial rows"></textarea>
			<p class="hint">
				{hasRows
					? 'The rows are encoded to Arrow and become the schema and first version of this table.'
					: DECLARE_ONLY_NOTE}
			</p>
			<div class="row">
				<button
					class="btn"
					type="submit"
					disabled={declBusy || declareHint !== null}
					aria-describedby={declareHint ? 'declare-hint' : undefined}
				>
					{declBusy ? '…' : hasRows ? 'Create with rows' : 'Declare'}
				</button>
				{#if declareHint}<span class="hint" id="declare-hint">{declareHint}</span>{/if}
			</div>
		</form>
	{/if}
	{#if declMsg}
		<div class="banner" class:ok={declMsg.ok} class:fail={!declMsg.ok}>{declMsg.text}</div>
	{/if}

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a
					href={resolve(`auth/login?redirect=${encodeURIComponent(page.url.pathname)}`)}>sign in</a
				> to browse the catalog.
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
					{ value: 'bronze', label: 'bronze' },
					{ value: 'silver', label: 'silver' },
					{ value: 'gold', label: 'gold' },
				]}
			/>
			<ProjectFilter value={projectFilter} onchange={onprojectchange} />
		</div>
		{#if filterFailure !== null}
			<ProjectFilterNotice
				project={projectFilter}
				reason={filterFailure}
				onclear={() => onprojectchange('')}
			/>
		{:else}
			<DataTable
				{table}
				loading={tables === null || filterWaits}
				emptyMessage={projectFilter
					? `No tables in project ${projectFilter}.`
					: 'No tables visible to you. The list holds only the tables your grants reach: ask a project admin for access, or declare one.'}
				onrowclick={openDrawer}
			/>
		{/if}
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
				<a
					class="mono jump"
					href={resolve('/catalog/namespaces/[id]', {
						id: encodeURIComponent(drawerRow.namespace),
					})}>{drawerRow.namespace}</a
				>
			</dd>
			<dt>medallion stage</dt>
			<dd>
				{#if drawerRow.stage}<StageBadge info={drawerRow.stage} />{:else}<span class="mut"
						>— (not a tier namespace)</span
					>{/if}
			</dd>
		</dl>
		<div class="jumps">
			<a
				class="btn"
				href={resolve('/catalog/tables/[table]', { table: encodeURIComponent(drawerRow.id) })}
				>Open detail</a
			>
			<!-- R18 table previewer: deep-link onto the detail pane's preview tab, which reads the
			     first N rows through the catalog's query door onto the shared data-table. -->
			<a
				class="btn"
				href={resolve('/catalog/tables/[table]?tab=preview', {
					table: encodeURIComponent(drawerRow.id),
				})}>Preview</a
			>
			<a
				class="btn"
				href={resolve('/catalog/tables/[table]?tab=access', {
					table: encodeURIComponent(drawerRow.id),
				})}>Access tab</a
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
		flex-direction: column;
		gap: 8px;
		margin-bottom: 12px;
	}
	.declare .row {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.declare input,
	.declare textarea {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
	}
	.declare input:disabled {
		opacity: 0.5;
	}
	.declare .loc {
		flex: 1;
		min-width: 220px;
	}
	.declare .rows {
		min-height: 56px;
		resize: vertical;
	}
	.hint {
		margin: 0;
		color: var(--faint);
		font-size: 12px;
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
