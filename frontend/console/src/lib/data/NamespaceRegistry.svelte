<script lang="ts">
	// `/namespaces` — the catalog's namespaces on the design system's DataTable (goal cond 4): one
	// sortable/searchable row per namespace, from each warehouse's own listing (the spec's
	// ListNamespaces on the warehouse's name) and from the table registry's ids, with the labels each
	// namespace carries (ADR 0211) and a filter by one, the #85 drop action (AlertDialog confirm;
	// Restrict by default, Cascade opt-in) and the create: the spec's CreateNamespace on
	// `<warehouse>$<name>`.
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
	} from '@riksarkivet/ui/data-table';
	import { Plus, RefreshCw, ShieldAlert, Trash2 } from '@lucide/svelte';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { fetchTables } from './remote/catalog.api';
	import NamespaceCreateForm, { type Banner } from './NamespaceCreateForm.svelte';
	import NamespaceDropDialog from './NamespaceDropDialog.svelte';
	import { listEstateNamespaces, type EstateNamespaces } from './estate-namespaces.ts';
	import ProjectFilter from './ProjectFilter.svelte';
	import ProjectFilterNotice from './ProjectFilterNotice.svelte';
	import { filterVerdict, projectOf, readFailure } from './project-filter.ts';
	import { ProjectIndexRead } from './project-index.svelte.ts';
	import RowDrawer from './RowDrawer.svelte';
	import { LabelIndexRead } from './label-index.svelte.ts';
	import LabelChips from './LabelChips.svelte';
	import LabelFilter from './LabelFilter.svelte';
	import LabelIndexNotice from './LabelIndexNotice.svelte';
	import { labelChips, type Labels } from './labels.ts';
	import { lineageTick, liveRead } from '#lib/live/tick.svelte.ts';
	import { parentNamespace, warehouseOf } from '#lib/api/identifiers.ts';
	import { Button } from '@riksarkivet/ui/button';
	import { catalogPath } from '#lib/catalog-trail.ts';
	import { CopyButton } from '@riksarkivet/ui/copy-button';

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
	let banner = $state<Banner | null>(null);

	/** The project the rows are filtered to (`''`: every project); the namespaces page keeps it in
	 *  its URL. */
	let {
		project: projectFilter,
		onprojectchange,
		label: labelFilter,
		onlabelchange,
	}: {
		project: string;
		onprojectchange: (project: string) => void;
		/** The label the rows are filtered to (`''`: any label); the page keeps it in its URL too. */
		label: string;
		onlabelchange: (label: string) => void;
	} = $props();

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
		void labels.load(projects.visible.state);
		if (res.ok) {
			tables = [...res.data.tables].sort();
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
		listing = listed.ok ? listed.data : null;
		listingFailure = listed.ok ? null : readFailure('warehouses', listed);
	}

	/** The labels each namespace carries, read per project the caller can see (ADR 0211). */
	const labels = new LabelIndexRead();

	// The table registry it folds and the namespace listings advance on one shared cursor, so the two
	// cannot disagree for longer than a tick about what exists.
	liveRead(lineageTick, () => load());

	// Group by the table's PARENT namespace — every segment but the LAST, via `parentNamespace`
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
		labels: Labels;
		warehouse: string;
		project: string | null;
	};

	const allRows = $derived.by((): Row[] => {
		const m = new Map<string, number>();
		// Seed with every LISTED namespace at zero, so one holding no tables still gets a row. A listing
		// that failed seeds nothing and the banner below says so — never silently "none".
		for (const ns of listing?.namespaces ?? []) m.set(ns, 0);
		for (const t of tables ?? []) {
			const ns = parentNamespace(t);
			m.set(ns, (m.get(ns) ?? 0) + 1);
		}
		return (
			[...m.entries()]
				// The warehouse is the first segment of the id; one with no registry record (the estate's
				// own) has no project, and shows a dash there.
				.map(([ns, count]): Row => ({
					ns,
					count,
					labels: labels.of('namespace', ns),
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
	const rows = $derived(
		allRows.filter(
			(r) =>
				(!labelFilter || labelFilter in r.labels) &&
				(!projectFilter || r.project === projectFilter),
		),
	);

	// The drop's confirm, mounted once per opening (`dropOpening` counts them), so its cascade choice
	// and its recovery read belong to that opening alone.
	let dropOpen = $state(false);
	let dropTarget = $state<string | null>(null);
	let dropOpening = $state(0);
	// Tables inside the namespace queued for drop — sizes the Cascade choice honestly.
	const targetCount = $derived(
		dropTarget === null ? 0 : (rows.find((r) => r.ns === dropTarget)?.count ?? 0),
	);

	function openDrop(ns: string): void {
		dropTarget = ns;
		dropOpening += 1;
		banner = null;
		dropOpen = true;
	}

	// The create, hidden behind the header's toggle so the page stays a list by default, as the
	// tables page's declare is.
	let creating = $state(false);

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
			id: 'labels',
			accessorFn: (r) => labelChips(r.labels).join(' '),
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'labels',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(labelsCell, row.original),
			meta: { headerClass: 'w-40' },
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
		href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(row.ns) })}
		>{catalogPath(row.ns)}</a
	>
{/snippet}
{#snippet projectCell(row: Row)}
	<!-- A monospace em dash, as the labels column draws one: a monospace em dash reads as an en dash. -->
	{#if row.project}<span class="mono">{row.project}</span>{:else}<span class="mut font-mono">—</span>{/if}
{/snippet}
{#snippet labelsCell(row: Row)}
	<LabelChips labels={row.labels} />
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
	<Button
		variant="ghost"
		size="xs"
		class="text-muted-foreground hover:text-destructive"
		aria-label={`Drop namespace ${row.ns}`}
		disabled={busy}
		onclick={() => openDrop(row.ns)}
	>
		<Trash2 size={12} /> drop
	</Button>
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
		<Button
			variant="outline"
			size="sm"
			class="ml-auto"
			aria-expanded={creating}
			aria-controls="create-namespace"
			onclick={() => (creating = !creating)}
		>
			<Plus size={12} /> New namespace
		</Button>
	</header>

	<NamespaceCreateForm
		open={creating && !unauthorized}
		warehouses={listing?.warehouses ?? []}
		bind:busy
		onresult={(next) => (banner = next)}
		onchanged={load}
	/>

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
			<LabelFilter value={labelFilter} names={labels.names} onchange={onlabelchange} />
		</div>
		{#if labels.state.kind === 'ready'}
			<LabelIndexNotice failures={labels.state.failures} filtering={labelFilter !== ''} />
		{/if}
		{#if verdict.kind === 'failed' || verdict.kind === 'unseen'}
			<ProjectFilterNotice
				project={projectFilter}
				reason={verdict.kind === 'failed' ? verdict.reason : null}
				onclear={() => onprojectchange('')}
			/>
		{:else}
			<DataTable
				{table}
				loading={tables === null ||
					verdict.kind === 'waiting' ||
					(labelFilter !== '' && labels.state.kind === 'loading')}
				emptyMessage={labelFilter
					? `No namespace you can see carries the label ${labelFilter}${projectFilter ? ` in project ${projectFilter}` : ''}.`
					: projectFilter
						? `No namespaces in project ${projectFilter}.`
						: 'No namespaces visible to you. The list holds only the namespaces your grants reach: ask a project admin for access, or create one in a warehouse you can write to.'}
				onrowclick={openDrawer}
			/>
		{/if}
	{/if}
</div>

<RowDrawer
	bind:open={drawerOpen}
	title={drawerRow ? catalogPath(drawerRow.ns) : ''}
	description="The registry record for this namespace (grouped from the table registry)."
>
	{#if drawerRow}
		<dl class="rec">
			<dt>namespace id</dt>
			<dd class="mono">{drawerRow.ns}<CopyButton value={drawerRow.ns} label="namespace id" /></dd>
			<dt>warehouse</dt>
			<dd class="mono">{drawerRow.warehouse}</dd>
			<dt>labels</dt>
			<dd><LabelChips labels={drawerRow.labels} /></dd>
			<dt>tables</dt>
			<dd class="mono">{drawerRow.count}</dd>
		</dl>
		<div class="jumps">
			<Button
				variant="outline"
				size="sm"
				href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(drawerRow.ns) })}
				>Open detail</Button
			>
		</div>
	{/if}
</RowDrawer>

{#if dropTarget !== null}
	{#key dropOpening}
		<NamespaceDropDialog
			bind:open={dropOpen}
			ns={dropTarget}
			count={targetCount}
			bind:busy
			onresult={(next) => (banner = next)}
			onchanged={load}
			ondone={() => (dropTarget = null)}
		/>
	{/key}
{/if}

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
		font-size: var(--text-xl);
		line-height: var(--text-xl--line-height);
		letter-spacing: var(--text-xl--letter-spacing);
		margin: 0;
	}
	.sub {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.banner {
		padding: 8px 12px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		margin-bottom: 12px;
		font-size: 13px;
	}
	.banner.ok {
		border-color: color-mix(in srgb, var(--success) 45%, var(--border));
		color: var(--success);
	}
	.banner.degraded p {
		margin: 0;
	}
	.banner.degraded {
		border-color: color-mix(in srgb, var(--warning) 50%, var(--border));
		background: color-mix(in srgb, var(--warning) 7%, transparent);
	}
	.banner.fail {
		border-color: color-mix(in srgb, var(--destructive) 45%, var(--border));
		color: var(--destructive);
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	.ns-name {
		color: var(--foreground);
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
		color: var(--muted-foreground);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--muted-foreground);
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
		color: var(--muted-foreground);
	}
	.rec dd {
		margin: 0;
		color: var(--foreground);
		word-break: break-all;
	}
	.jumps {
		display: flex;
		gap: 8px;
	}
</style>
