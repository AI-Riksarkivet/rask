<script lang="ts">
	// `/tables` — the catalog table registry (#52) on the design system's DataTable (goal cond 4):
	// sortable columns, a text search, a project filter and a LABEL filter (the labels each table
	// carries, ADR 0211, shown as chips per row). Same stack-mode states as before: governed without a
	// session ⇒ sign-in, unreachable ⇒ retrying, open ⇒ data or the honest empty state.
	// The #85 declare form, and its create-with-rows second half, is `TableDeclareForm`.
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
	import { Plus, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { Button } from '@riksarkivet/ui/button';
	import { fetchTables } from './remote/catalog.api';
	import TableDeclareForm from './TableDeclareForm.svelte';
	import ProjectFilter from './ProjectFilter.svelte';
	import ProjectFilterNotice from './ProjectFilterNotice.svelte';
	import { filterVerdict, projectOf } from './project-filter.ts';
	import { ProjectIndexRead } from './project-index.svelte.ts';
	import RowDrawer from './RowDrawer.svelte';
	import { parentNamespace } from '#lib/api/identifiers.ts';
	import { LabelIndexRead } from './label-index.svelte.ts';
	import LabelChips from './LabelChips.svelte';
	import LabelFilter from './LabelFilter.svelte';
	import LabelIndexNotice from './LabelIndexNotice.svelte';
	import { labelChips, type Labels } from './labels.ts';
	import { catalogPath } from '#lib/catalog-trail.ts';
	import { CopyButton } from '@riksarkivet/ui/copy-button';
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
		label: labelFilter,
		onlabelchange,
		frame = 'page',
	}: {
		project: string;
		onprojectchange: (project: string) => void;
		/** The label the rows are filtered to (`''`: any label), held where the project filter is. */
		label: string;
		onlabelchange: (label: string) => void;
		/** Where the registry is drawn: its own page (the page's h1, its column and padding), or a
		 *  workbench panel (a heading under the workbench's h1, the panel's whole width). */
		frame?: 'page' | 'panel';
	} = $props();

	// Each row's project, its warehouse's (the first segment of its id); the filter narrows the rows
	// this caller was given, a view and never a boundary.
	const projects = new ProjectIndexRead();

	const unauthorized = $derived(tables === null && lastStatus === 401);
	const offline = $derived(tables === null && settled && lastStatus !== 401);

	// The declare form, hidden behind a toggle so the registry stays a list by default.
	let declaring = $state(false);

	/** The labels each listed table carries, read per project the caller can see (ADR 0211). */
	const labels = new LabelIndexRead();

	async function load(): Promise<void> {
		const [res] = await Promise.all([fetchTables(), projects.load()]);
		settled = true;
		void labels.load(projects.visible.state);
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

	/** The form's toggle, held to hand the focus back to when Escape closes the form. */
	let declareToggle = $state<HTMLElement | null>(null);
	function closeDeclare(): void {
		declaring = false;
		declareToggle?.focus();
	}

	// ── the DataTable (goal cond 4) ──
	type Row = { id: string; namespace: string; labels: Labels; project: string | null };
	const allRows = $derived.by((): Row[] => {
		const all = (tables ?? []).map((id): Row => ({
			id,
			namespace: parentNamespace(id),
			labels: labels.of('table', id),
			project: projects.index ? projectOf(projects.index, id) : null,
		}));
		return all;
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

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

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
		href={resolve('/catalog/tables/[table]', { table: encodeURIComponent(row.id) })}
		>{catalogPath(row.id)}</a
	>
{/snippet}
{#snippet projectCell(row: Row)}
	<!-- A monospace em dash, as the labels column draws one: a monospace em dash reads as an en dash. -->
	{#if row.project}<span class="mono">{row.project}</span>{:else}<span class="mut font-mono">—</span>{/if}
{/snippet}
{#snippet labelsCell(row: Row)}
	<LabelChips labels={row.labels} />
{/snippet}
{#snippet nsCell(row: Row)}
	<a
		class="nslink mono"
		href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(row.namespace) })}
		>{catalogPath(row.namespace)}</a
	>
{/snippet}

<div class={['page', { panel: frame === 'panel' }]}>
	<header>
		<svelte:element this={frame === 'page' ? 'h1' : 'h2'} class="title">Tables</svelte:element>
		<span class="sub mono"
			>{projectFilter
				? `the catalog registry, in project ${projectFilter} — the tables your grants allow there`
				: 'the catalog registry, estate-wide — every table your grants allow'}</span
		>
		{#if !unauthorized}
			<Button
				variant="outline"
				size="sm"
				class="ml-auto"
				bind:ref={declareToggle}
				onclick={() => (declaring = !declaring)}
			>
				<Plus size={12} /> Declare table
			</Button>
		{/if}
	</header>

	<TableDeclareForm open={declaring && !unauthorized} onclose={closeDeclare} onchanged={load} />

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
					? `No table you can see carries the label ${labelFilter}${projectFilter ? ` in project ${projectFilter}` : ''}.`
					: projectFilter
						? `No tables in project ${projectFilter}.`
						: 'No tables visible to you. The list holds only the tables your grants reach: ask a project admin for access, or declare one.'}
				onrowclick={openDrawer}
			/>
		{/if}
	{/if}
</div>

<RowDrawer
	bind:open={drawerOpen}
	title={drawerRow ? catalogPath(drawerRow.id) : ''}
	description="The registry record for this table."
>
	{#if drawerRow}
		<dl class="rec">
			<dt>table id</dt>
			<dd class="mono">{drawerRow.id}<CopyButton value={drawerRow.id} label="table id" /></dd>
			<dt>namespace</dt>
			<dd>
				<a
					class="mono jump"
					href={resolve('/catalog/namespaces/[id]', {
						id: encodeURIComponent(drawerRow.namespace),
					})}>{catalogPath(drawerRow.namespace)}</a
				>
			</dd>
			<dt>labels</dt>
			<dd><LabelChips labels={drawerRow.labels} /></dd>
		</dl>
		<div class="jumps">
			<Button
				variant="outline"
				size="sm"
				href={resolve('/catalog/tables/[table]', { table: encodeURIComponent(drawerRow.id) })}
				>Open detail</Button
			>
			<!-- R18 table previewer: deep-link onto the detail pane's preview tab, which reads the
			     first N rows through the catalog's query door onto the shared data-table. -->
			<Button
				variant="outline"
				size="sm"
				href={resolve('/catalog/tables/[table]?tab=preview', {
					table: encodeURIComponent(drawerRow.id),
				})}>Preview</Button
			>
			<Button
				variant="outline"
				size="sm"
				href={resolve('/catalog/tables/[table]?tab=access', {
					table: encodeURIComponent(drawerRow.id),
				})}>Access tab</Button
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
	/* A panel is already framed by the dock: its width is the panel's, with a slim inset. */
	.page.panel {
		max-width: none;
		padding: 12px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 12px;
		margin-bottom: 18px;
	}
	.title {
		font-size: var(--text-xl);
		line-height: var(--text-xl--line-height);
		letter-spacing: var(--text-xl--letter-spacing);
		margin: 0;
	}
	.panel .title {
		font-size: 15px;
	}
	.sub {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	.rowlink {
		color: var(--foreground);
		text-decoration: none;
	}
	.rowlink:hover {
		text-decoration: underline;
	}
	.nslink {
		color: var(--muted-foreground);
		text-decoration: none;
	}
	.nslink:hover {
		color: var(--foreground);
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
	.jump {
		color: var(--foreground);
	}
	.jumps {
		display: flex;
		gap: 8px;
	}
</style>
