<script lang="ts">
	// `/namespaces` — the catalog's namespaces on the shared @rask/ui DataTable (goal cond 4): one
	// sortable/searchable row per namespace, from each warehouse's own listing (the spec's
	// ListNamespaces on the warehouse's name) and from the table registry's ids, with the medallion
	// tier badge (goal cond 3), the #85 drop action (AlertDialog confirm; Restrict by default, Cascade
	// opt-in) and the create: the spec's CreateNamespace on `<warehouse>$<name>`.
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
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { fetchTables } from './remote/catalog.api';
	import { dropNamespace } from './remote/namespace.api';
	import { dropRecoveryText } from './drop-recovery';
	import { DropRecoveryRead } from './drop-recovery-read.svelte.ts';
	import { createWarehouseNamespace } from './remote/warehouses.api';
	import { listEstateNamespaces, type EstateNamespaces } from './estate-namespaces.ts';
	import ProjectFilter from './ProjectFilter.svelte';
	import ProjectFilterNotice from './ProjectFilterNotice.svelte';
	import { filterVerdict, projectOf, readFailure } from './project-filter.ts';
	import { ProjectIndexRead } from './project-index.svelte.ts';
	import RowDrawer from './RowDrawer.svelte';
	import { namespaceOfTable, stageOf, type StageInfo } from './stage';
	import StageBadge from './StageBadge.svelte';
	import { lineageTick, liveRead } from '#lib/live/tick.svelte.ts';
	import { DELIMITER, warehouseOf } from '#lib/api/identifiers.ts';
	import { missingFieldsHint } from '#lib/form-hint.ts';
	import { Select } from '@rask/ui/select';

	let tables = $state<string[] | null>(null);
	// Each warehouse's own listing — the same listing a warehouse delete reads for what it still holds.
	// Without it this page would list only namespaces that already hold a table, so an EMPTY one would
	// be invisible: exactly the object someone came here to find and drop (#66). A listing that failed
	// is its own state, said in the banner below, never collapsed into "none".
	let listing = $state.raw<EstateNamespaces | null>(null);
	let listingFailure = $state<string | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let busy = $state(false);
	let banner = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);
	let dropOpen = $state(false);
	let dropTarget = $state<string | null>(null);
	let cascade = $state(false);

	/** The project the rows are filtered to (`''`: every project); the namespaces page keeps it in
	 *  its URL. */
	let {
		project: projectFilter,
		onprojectchange,
	}: { project: string; onprojectchange: (project: string) => void } = $props();

	// Each row's project, its warehouse's (the first segment of its id); the filter narrows the rows
	// this caller was given, a view and never a boundary.
	const projects = new ProjectIndexRead();

	const unauthorized = $derived(tables === null && lastStatus === 401);
	const offline = $derived(tables === null && settled && lastStatus !== 401);

	async function load(): Promise<void> {
		// Independent reads, so they go together: the table registry supplies COUNTS, the warehouses'
		// listings supply what EXISTS.
		const [res, listed] = await Promise.all([
			fetchTables(),
			listEstateNamespaces(),
			projects.load(),
		]);
		settled = true;
		if (res.ok) {
			tables = [...res.data.tables].sort();
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
		listing = listed.ok ? listed.data : null;
		listingFailure = listed.ok ? null : readFailure('warehouses', listed);
	}

	// The table registry it folds and the namespace listings advance on one shared cursor, so the two
	// cannot disagree for longer than a tick about what exists.
	liveRead(lineageTick, () => load());

	// Group by the table's PARENT namespace — every segment but the LAST, via `namespaceOfTable`
	// (`#lib/api/identifiers.ts`). A bare name with no delimiter is its own root, which is this view's
	// rule and not the backend's: `parent_namespace_id` returns None there, so a root-level table
	// gets a row here rather than vanishing. That divergence is safe because this answer is only a
	// grouping key; anywhere it would become an FGA object id, `parentNamespaceOrNull` is the one to use.
	//
	// The split is at the LAST `$`, never the first: namespaces NEST, so `acme$bronze$pages` belongs to
	// `acme$bronze`, and folding it under `acme` would file it beneath an object that is not its parent.
	type Row = {
		ns: string;
		count: number;
		stage: StageInfo | null;
		warehouse: string;
		project: string | null;
	};

	const allRows = $derived.by((): Row[] => {
		const m = new Map<string, number>();
		// Seed with every LISTED namespace at zero, so one holding no tables still gets a row. A listing
		// that failed seeds nothing and the banner below says so — never silently "none".
		for (const ns of listing?.namespaces ?? []) m.set(ns, 0);
		for (const t of tables ?? []) {
			const ns = namespaceOfTable(t);
			m.set(ns, (m.get(ns) ?? 0) + 1);
		}
		return (
			[...m.entries()]
				// The warehouse is the first segment of the id; one with no registry record (the estate's
				// own) has no project, and shows a dash there.
				.map(([ns, count]): Row => ({
					ns,
					count,
					stage: stageOf(ns),
					warehouse: warehouseOf(ns),
					project: projects.index ? projectOf(projects.index, ns) : null,
				}))
				.sort((a, b) => a.ns.localeCompare(b.ns))
		);
	});
	// What the filter does, from the rows the catalog answered and the projects the caller can see.
	const rowProjects = $derived(new Set(allRows.flatMap((r) => (r.project ? [r.project] : []))));
	const verdict = $derived(
		filterVerdict({
			filter: projectFilter,
			index: projects.state,
			visible: projects.visible.state,
			rowProjects,
		}),
	);
	const rows = $derived(allRows.filter((r) => !projectFilter || r.project === projectFilter));

	// Tables inside the namespace queued for drop — sizes the Cascade choice honestly.
	const targetCount = $derived(
		dropTarget === null ? 0 : (rows.find((r) => r.ns === dropTarget)?.count ?? 0),
	);

	// What a cascade would leave recoverable, read from the namespace's warehouse when the dialog opens.
	const recovery = new DropRecoveryRead();

	function openDrop(ns: string): void {
		dropTarget = ns;
		cascade = false;
		banner = null;
		dropOpen = true;
		void recovery.load(ns);
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

	// The create: a namespace directly under a warehouse, named `<warehouse>$<name>`. Hidden behind
	// the header's toggle so the page stays a list by default, as the tables page's declare is.
	let creating = $state(false);
	let createDraft = $state({ warehouse: '', name: '' });
	const createHint = $derived(
		createDraft.name.includes(DELIMITER)
			? `A name holds no ${DELIMITER}: this creates a namespace directly under the warehouse.`
			: missingFieldsHint('Creating', [
					['a warehouse', createDraft.warehouse],
					['a name', createDraft.name.trim()],
				]),
	);

	async function create(): Promise<void> {
		const warehouse = createDraft.warehouse;
		const name = createDraft.name.trim();
		if (busy || createHint !== null) return;
		busy = true;
		banner = null;
		try {
			const res = await createWarehouseNamespace({ warehouse, name });
			if (res.ok) {
				banner = { tone: 'ok', text: `namespace ${warehouse}${DELIMITER}${name} created` };
				createDraft = { warehouse, name: '' };
				await load();
			} else if (res.status === 401)
				banner = { tone: 'fail', text: 'Sign in — creating a namespace is a per-user action.' };
			else if (res.status === 403)
				banner = {
					tone: 'fail',
					text: `Denied: creating a namespace in ${warehouse} needs write access there (can_create_namespace). ${res.detail}`,
				};
			else if (res.status === 0)
				banner = {
					tone: 'fail',
					text: 'Not confirmed: the catalog did not answer, so the namespace may have been created.',
				};
			else banner = { tone: 'fail', text: res.detail };
		} finally {
			busy = false;
		}
	}

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	// Goal cond 8: a row click opens the record drawer; a click on a link or a button in a cell is that
	// control's (the data table leaves a click on a control to the control).
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
			id: 'project',
			accessorFn: (r) => r.project ?? '',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'project',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(projectCell, row.original),
			meta: { headerClass: 'w-32' },
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
			// WHICH WAREHOUSE HOLDS THIS: the first segment of the namespace's id, read off the id itself.
			id: 'warehouse',
			accessorKey: 'warehouse',
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
		href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(row.ns) })}>{row.ns}</a
	>
{/snippet}
{#snippet projectCell(row: Row)}
	<!-- The stage column's dash, in the stage column's face: a monospace em dash reads as an en dash. -->
	{#if row.project}<span class="mono">{row.project}</span>{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet stageCell(row: Row)}
	{#if row.stage}<StageBadge info={row.stage} />{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet warehouseCell(row: Row)}
	<!-- A link, because the warehouse has a detail page, the estate's own included. -->
	<a
		class="wh"
		href={resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(row.warehouse) })}
	>
		{row.warehouse}
	</a>
{/snippet}
{#snippet actionsCell(row: Row)}
	<button
		class="drop"
		aria-label={`Drop namespace ${row.ns}`}
		disabled={busy}
		onclick={() => openDrop(row.ns)}
	>
		<Trash2 size={12} /> drop
	</button>
{/snippet}

<div class="page">
	<header>
		<h1>Namespaces</h1>
		<span class="sub mono"
			>{projectFilter
				? `in project ${projectFilter} — the namespaces your grants allow there`
				: 'every warehouse, estate-wide — every namespace your grants allow'} ·
			&lt;warehouse&gt;$&lt;namespace&gt;</span
		>
		<button
			class="new"
			aria-expanded={creating}
			aria-controls="create-namespace"
			onclick={() => (creating = !creating)}
		>
			<Plus size={12} /> New namespace
		</button>
	</header>

	{#if creating && !unauthorized}
		<form
			id="create-namespace"
			class="create"
			aria-label="Create namespace"
			onsubmit={(e) => {
				e.preventDefault();
				void create();
			}}
		>
			<label class="field">
				Warehouse
				<Select
					bind:value={createDraft.warehouse}
					ariaLabel="Warehouse"
					placeholder="warehouse…"
					options={(listing?.warehouses ?? []).map((w) => ({ value: w, label: w }))}
				/>
			</label>
			<label class="field">
				Name
				<input class="mono" bind:value={createDraft.name} placeholder="bronze" />
			</label>
			<button
				class="jbtn"
				type="submit"
				disabled={busy || createHint !== null}
				aria-describedby={createHint ? 'create-hint' : undefined}
			>
				Create
			</button>
			{#if createHint}<p class="mut hint" id="create-hint">{createHint}</p>{/if}
		</form>
	{/if}

	<!-- #86: a listing that failed is its own state. Collapsing it into "no namespaces" is the #66
	     defect — an empty namespace invisible with nothing said. -->
	{#if settled && !unauthorized && !offline && (listingFailure || (listing?.unread.length ?? 0) > 0)}
		<!-- Its OWN class, not `.banner.fail`: that one means "the action you just took failed", this
		     means "a read this page depends on is unavailable". -->
		<div class="banner degraded" data-testid="namespaces-partial">
			{#if listingFailure}
				The warehouses could not be listed ({listingFailure}), so this list shows only namespaces
				that hold a table: an empty one may be missing.
			{:else}
				{#each listing?.unread ?? [] as u (u.warehouse)}
					<p>
						The namespaces of warehouse <span class="mono">{u.warehouse}</span> could not be listed
						({u.reason}), so an empty namespace in it may be missing here.
					</p>
				{/each}
			{/if}
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
				This stack is governed — <a
					href={resolve(`auth/login?redirect=${encodeURIComponent(page.url.pathname)}`)}>sign in</a
				> to browse namespaces.
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
			<ProjectFilter
				value={projectFilter}
				visible={projects.visible.ids}
				ofRows={[...rowProjects]}
				onchange={onprojectchange}
			/>
		</div>
		{#if verdict.kind === 'failed' || verdict.kind === 'unseen'}
			<ProjectFilterNotice
				project={projectFilter}
				reason={verdict.kind === 'failed' ? verdict.reason : null}
				onclear={() => onprojectchange('')}
			/>
		{:else}
			<DataTable
				{table}
				loading={tables === null || verdict.kind === 'waiting'}
				emptyMessage={projectFilter
					? `No namespaces in project ${projectFilter}.`
					: 'No namespaces visible to you. The list holds only the namespaces your grants reach: ask a project admin for access, or create one in a warehouse you can write to.'}
				onrowclick={openDrawer}
			/>
		{/if}
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
			<dt>warehouse</dt>
			<dd class="mono">{drawerRow.warehouse}</dd>
			<dt>medallion stage</dt>
			<dd>
				{#if drawerRow.stage}<StageBadge info={drawerRow.stage} />{:else}<span class="mut"
						>— (not a tier namespace)</span
					>{/if}
			</dd>
			<dt>tables</dt>
			<dd class="mono">{drawerRow.count}</dd>
		</dl>
		<div class="jumps">
			<a
				class="jbtn"
				href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(drawerRow.ns) })}
				>Open detail</a
			>
		</div>
	{/if}
</RowDrawer>

<AlertDialog.Root bind:open={dropOpen}>
	<AlertDialog.Content>
		<AlertDialog.Title>Drop namespace {dropTarget}</AlertDialog.Title>
		<AlertDialog.Description>
			This drops <span class="mono">{dropTarget}</span> from the catalog (owner-gated: can_delete).
			The default Restrict behavior refuses a non-empty namespace, and removes an empty one
			permanently: there is nothing in it to recover. Tick Cascade to also drop everything inside
			it.
			{#if cascade}
				A cascade drops it and its tables
				{#if recovery.current}{dropRecoveryText(recovery.current)}.{:else}(reading how long they
					stay recoverable…).{/if}
			{/if}
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
		cursor: pointer;
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
	.banner.degraded p {
		margin: 0;
	}
	.banner.degraded {
		border-color: color-mix(in srgb, var(--warn) 50%, var(--line));
		background: color-mix(in srgb, var(--warn) 7%, transparent);
	}
	.banner.fail {
		border-color: color-mix(in srgb, var(--fail) 45%, var(--line));
		color: var(--fail);
	}
	.create {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-end;
		gap: 8px;
		margin-bottom: 12px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		color: var(--mut);
		font-size: 12px;
	}
	.create input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 5px 9px;
		font-size: 12px;
	}
	.hint {
		flex-basis: 100%;
		margin: 0;
		font-size: 12px;
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
