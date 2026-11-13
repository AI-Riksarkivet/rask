<script lang="ts">
	// `/warehouses` — the #3-A control plane (#52 UI): provisioned warehouses (bucket-per-warehouse
	// physical tenancy), the activate/deactivate lifecycle, and provisioning. Reads are
	// session-forwarded; writes are project-admin gated by the catalog (can_create_warehouse /
	// can_administer) — a non-admin sees the denial banner, never a silent no-op. A warehouse git
	// declares offers no lifecycle control (its doors refuse them), and the estate's own warehouse,
	// which has no registry record, is listed with none at all.
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
	} from '@rask/ui/data-table';
	import { RefreshCw, ShieldAlert, Warehouse as WarehouseIcon } from '@lucide/svelte';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import type { Warehouse, WarehouseRecord } from './catalog';
	import {
		createWarehouse,
		fetchServedWarehouses,
		fetchWarehouses,
		setWarehouseActive,
	} from './remote/warehouses.api';
	import DeclaredInGit, { declaredInGit, DECLARED_IN_GIT_REASON } from './DeclaredInGit.svelte';
	import ProjectFilter from './ProjectFilter.svelte';
	import ProjectFilterNotice from './ProjectFilterNotice.svelte';
	import { VisibleProjects } from './project-index.svelte.ts';
	import { filterVerdict } from './project-filter.ts';
	import RowDrawer from './RowDrawer.svelte';
	import WarehouseDeleteDialog from './WarehouseDeleteDialog.svelte';
	import { controlTick, liveRead } from '#lib/live/tick.svelte.ts';
	import { missingFieldsHint } from '#lib/form-hint.ts';

	/** A registered warehouse, or the estate's own (`estate`): one the root listing answers and the
	 *  registry does not, which holds the lakehouse's own namespaces and belongs to no project. */
	type Row = WarehouseRecord & { estate?: boolean };

	let warehouses = $state<WarehouseRecord[] | null>(null);
	let estateIds = $state<string[]>([]);
	let lastStatus = $state(0);
	let settled = $state(false);
	let busy = $state(false);
	let banner = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);
	let draft = $state({ id: '', project: '', bucket: '' });

	const provisionHint = $derived(
		missingFieldsHint('Provisioning', [
			['a warehouse id', draft.id],
			['a project', draft.project],
		]),
	);

	const unauthorized = $derived(warehouses === null && lastStatus === 401);
	const offline = $derived(warehouses === null && settled && lastStatus !== 401);

	// A pre-lifecycle warehouse (provisioned before the activate/deactivate feature) has no `status`
	// field — treat that as active, so it renders active with the correct toggle direction.
	function statusOf(w: Warehouse): string {
		return w.status ?? 'active';
	}

	async function load(): Promise<void> {
		const [res, served] = await Promise.all([
			fetchWarehouses(),
			fetchServedWarehouses(),
			visible.load(),
		]);
		settled = true;
		if (res.ok) {
			warehouses = res.data;
			lastStatus = 200;
			// Only against a registry that answered: against a failed one every served warehouse would
			// read as the estate's own.
			const registered = new Set(res.data.map((w) => w.id));
			estateIds = served.ok ? served.data.namespaces.filter((id) => !registered.has(id)) : [];
		} else {
			lastStatus = res.status;
		}
	}

	// The CONTROL cursor, not the lineage one, and this is the surface that shows why the distinction is
	// real: provisioning or deactivating a warehouse writes no dataset and
	// therefore emits no lineage event — the lineage cursor would never move for any of it. Warehouse
	// lifecycle is a catalog control-plane change, so that is the feed that reports it.
	//
	// The control feed is estate-admin gated (`can_observe_events` on the FGA root) while these READS are
	// open to any signed-in user, so a non-admin simply never ticks: they get the initial read plus the
	// re-read every mutation below already performs. That is the honest degradation — a non-admin cannot
	// change any of this, and the writes are project-admin gated anyway.
	liveRead(controlTick, () => load());

	function fail(status: number, detail: string): void {
		if (status === 401)
			banner = { tone: 'fail', text: 'Sign in — warehouse admin is a per-user action.' };
		else if (status === 403 || status === 404)
			// The lifecycle ops hide existence from non-admins (a denied deactivate is a 404, not a 403),
			// so both map to the same admin-required message; the catalog's own words follow, as a 403
			// for a git-declared warehouse names git.
			banner = {
				tone: 'fail',
				text: `Denied: warehouse admin needs the project-admin rung. ${detail}`.trim(),
			};
		else banner = { tone: 'fail', text: detail };
	}

	async function provision(): Promise<void> {
		if (busy || !draft.id.trim() || !draft.project.trim()) return;
		busy = true;
		banner = null;
		try {
			const res = await createWarehouse({
				id: draft.id.trim(),
				project: draft.project.trim(),
				bucket: draft.bucket.trim() || null,
			});
			if (res.ok) {
				banner = {
					tone: 'ok',
					text: `warehouse ${res.data.id} provisioned (${res.data.root_uri})`,
				};
				draft = { id: '', project: '', bucket: '' };
				await load();
			} else {
				fail(res.status, res.detail);
			}
		} finally {
			busy = false;
		}
	}

	async function toggleActive(w: Warehouse): Promise<void> {
		if (busy) return;
		busy = true;
		banner = null;
		try {
			const res = await setWarehouseActive({ id: w.id, active: statusOf(w) !== 'active' });
			if (res.ok) await load();
			else fail(res.status, res.detail);
		} finally {
			busy = false;
		}
	}

	// ── the DataTable (goal cond 4): sortable/searchable warehouse rows, id linking into the
	// hierarchy's warehouse page, the activate/deactivate lifecycle action preserved. ──
	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	// Goal cond 8: a row click opens the full-record drawer; a click on the link or a lifecycle button
	// is that control's (the data table leaves a click on a control to the control).
	let drawerOpen = $state(false);
	let drawerRow = $state<Row | null>(null);
	function openDrawer(row: Row): void {
		drawerRow = row;
		drawerOpen = true;
	}

	// The destroy door. It gets its own dialog rather than a banner-confirm because its three opt-ins
	// (cascade / purge_bucket / force) each need to be argued for separately — see
	// `WarehouseDeleteDialog`. The list re-reads on `ondeleted` for immediacy; the catalog also emits a
	// `warehouse_deleted` control event, so the `liveRead(controlTick, …)` above re-reads every OTHER
	// open warehouse list on the estate too.
	let deleteOpen = $state(false);
	let deleteRow = $state<WarehouseRecord | null>(null);
	function openDelete(row: WarehouseRecord): void {
		deleteRow = row;
		deleteOpen = true;
	}

	const columns: ColumnDef<Row>[] = [
		{
			id: 'id',
			accessorKey: 'id',
			header: sortableHeader('id'),
			cell: ({ row }) => renderSnippet(idCell, row.original),
		},
		{
			id: 'project',
			accessorKey: 'project',
			header: sortableHeader('project'),
			cell: ({ row }) => renderSnippet(projectCell, row.original),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'bucket',
			accessorKey: 'bucket',
			header: sortableHeader('bucket'),
			cell: ({ row }) => renderSnippet(bucketCell, row.original),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'status',
			accessorFn: (w) => (w.estate ? '' : statusOf(w)),
			header: sortableHeader('status'),
			cell: ({ row }) => renderSnippet(statusCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { headerClass: 'w-44', cellClass: 'text-right' },
		},
	];

	/** The project the rows are filtered to (`''`: every project); the warehouses page keeps it in its
	 *  URL. A warehouse names its project, so the filter needs no other read. It narrows the rows this
	 *  caller was given; it is a view, not a boundary. */
	let {
		project: projectFilter,
		onprojectchange,
	}: { project: string; onprojectchange: (project: string) => void } = $props();
	// The projects the caller can see, so a filter naming none of them, and no warehouse's, says so
	// rather than reading as a project with no warehouse.
	const visible = new VisibleProjects();
	const rowProjects = $derived(new Set((warehouses ?? []).map((w) => w.project)));
	const verdict = $derived(
		filterVerdict({ filter: projectFilter, index: null, visible: visible.state, rowProjects }),
	);
	// The estate's own warehouse is listed with every project and under none, so a project filter
	// leaves it out.
	const shown = $derived.by((): Row[] => {
		const estate: Row[] = estateIds.map((id) => ({
			id,
			project: '',
			bucket: '',
			root_uri: '',
			estate: true,
		}));
		const all: Row[] = [...(warehouses ?? []), ...estate];
		return projectFilter ? all.filter((w) => w.project === projectFilter) : all;
	});

	const whTable = createSvelteTable({
		get data() {
			return shown;
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

{#snippet idCell(w: Row)}
	<span class="idcell">
		<a
			class="whlink mono"
			href={resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(w.id) })}>{w.id}</a
		>
		{#if w.estate}
			<span class="mut">the estate’s own</span>
		{:else if declaredInGit(w)}
			<DeclaredInGit id={`git-${w.id}`} />
		{/if}
	</span>
{/snippet}
{#snippet projectCell(w: Row)}
	{#if w.estate}<span class="mut">—</span>{:else}{w.project}{/if}
{/snippet}
{#snippet bucketCell(w: Row)}
	{#if w.estate}<span class="mut">—</span>{:else}{w.bucket}{/if}
{/snippet}
{#snippet statusCell(w: Row)}
	{#if w.estate}
		<span class="mut">—</span>
	{:else}
		<span class="chip mono" class:off={statusOf(w) !== 'active'}>{statusOf(w)}</span>
	{/if}
{/snippet}
{#snippet actionsCell(w: Row)}
	<!-- The estate's own warehouse has no registry record, so no lifecycle door answers for it. A
	     git-declared one keeps its controls on screen, disabled, with the reason as their description:
	     its doors answer 403, and offering what will be refused is the defect. -->
	{#if !w.estate}
		{@const git = declaredInGit(w)}
		<button
			class="btn ghost"
			disabled={busy || git}
			title={git ? DECLARED_IN_GIT_REASON : undefined}
			aria-describedby={git ? `git-${w.id}` : undefined}
			onclick={() => toggleActive(w)}
		>
			{statusOf(w) === 'active' ? 'deactivate' : 'activate'}
		</button>
		<button
			class="btn ghost destroy"
			disabled={busy || git}
			title={git ? DECLARED_IN_GIT_REASON : undefined}
			aria-describedby={git ? `git-${w.id}` : undefined}
			aria-label={`Delete warehouse ${w.id}`}
			onclick={() => openDelete(w)}
		>
			delete
		</button>
	{/if}
{/snippet}

<div class="page">
	<header>
		<WarehouseIcon size={16} />
		<h1>Warehouses</h1>
		<span class="sub mono"
			>{projectFilter
				? `in project ${projectFilter} — the warehouses your grants allow there`
				: 'every warehouse your grants allow, across projects'} · bucket-per-warehouse tenancy ·
			project-admin gated</span
		>
	</header>

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
				> to view warehouses.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}) — retrying.</p>
		</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Search warehouses…" />
			<ProjectFilter
				value={projectFilter}
				visible={visible.ids}
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
				table={whTable}
				loading={warehouses === null || verdict.kind === 'waiting'}
				emptyMessage={projectFilter
					? `No warehouses in project ${projectFilter}.`
					: 'No warehouses visible to you. The list holds only the warehouses your grants reach: ask a project admin for access, or provision one below.'}
				onrowclick={openDrawer}
			/>
		{/if}

		<section>
			<h2>Provision</h2>
			<form
				class="row"
				onsubmit={(e) => {
					e.preventDefault();
					provision();
				}}
			>
				<!-- Each field named on screen, not by a placeholder that goes when the reader types. -->
				<label class="text-muted-foreground flex flex-col gap-1 text-xs">
					Warehouse id
					<input class="mono" bind:value={draft.id} placeholder="acme-wh" />
				</label>
				<label class="text-muted-foreground flex flex-col gap-1 text-xs">
					Project
					<input class="mono" bind:value={draft.project} placeholder="acme" />
				</label>
				<label class="text-muted-foreground flex flex-col gap-1 text-xs">
					Bucket
					<input class="mono" bind:value={draft.bucket} placeholder="defaults to the id" />
				</label>
				<button
					class="btn"
					type="submit"
					disabled={busy || provisionHint !== null}
					aria-describedby={provisionHint ? 'provision-hint' : undefined}
				>
					Provision
				</button>
			</form>
			{#if provisionHint}<p class="mut" id="provision-hint">{provisionHint}</p>{/if}
		</section>
	{/if}
</div>

<RowDrawer
	bind:open={drawerOpen}
	title={drawerRow?.id ?? ''}
	description={drawerRow?.estate
		? 'The estate’s own warehouse: no registry record.'
		: 'The full warehouse registry record.'}
>
	{#if drawerRow?.estate}
		<p class="mut">
			The estate’s own warehouse holds the lakehouse’s own namespaces (its provenance). It belongs
			to no project and has no registry record, so it has no bucket, status or lifecycle to show
			here, and it cannot be deactivated or deleted.
		</p>
		<div class="jumps">
			<a
				class="jbtn"
				href={resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(drawerRow.id) })}
				>Open detail</a
			>
		</div>
	{:else if drawerRow}
		<dl class="rec">
			<dt>warehouse id</dt>
			<dd class="mono">{drawerRow.id}</dd>
			<dt>project</dt>
			<dd>
				<!-- The project rung is the TOP of the hierarchy and lives in the console (2026-08-03
				     ruling); the console owns warehouse and below. So the href is absolute rather than
				     base-relative, and it MUST hard-navigate — a soft nav would land in a route the console
				     does not own. -->
				<a
					class="mono jump"
					href={resolve('/projects/[project]', { project: encodeURIComponent(drawerRow.project) })}
					>{drawerRow.project}</a
				>
			</dd>
			<dt>bucket</dt>
			<dd class="mono">{drawerRow.bucket}</dd>
			<dt>root uri</dt>
			<dd class="mono">{drawerRow.root_uri}</dd>
			<dt>status</dt>
			<dd>
				<span class="chip mono" class:off={statusOf(drawerRow) !== 'active'}
					>{statusOf(drawerRow)}</span
				>
			</dd>
			<dt>class</dt>
			<dd>
				{#if drawerRow.serving === 'gold'}
					<!-- the serving marker: this record hosts the project's gold tier in its own bucket -->
					<span class="chip gold mono">serving · gold</span>
				{:else}
					<span class="mut">work warehouse</span>
				{/if}
			</dd>
			<dt>created</dt>
			<dd class="mono">{drawerRow.created_at ?? '—'}</dd>
			{#if declaredInGit(drawerRow)}
				<dt>managed by</dt>
				<dd><DeclaredInGit /></dd>
			{/if}
		</dl>
		<div class="jumps">
			<a
				class="jbtn"
				href={resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(drawerRow.id) })}
				>Open detail</a
			>
			<!-- full-page, same reason as the project cell above -->
			<a
				class="jbtn"
				href={resolve('/projects/[project]', { project: encodeURIComponent(drawerRow.project) })}
				>Open project</a
			>
		</div>
	{/if}
</RowDrawer>

<WarehouseDeleteDialog bind:open={deleteOpen} warehouse={deleteRow} ondeleted={() => load()} />

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 18px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 18px 0 8px;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
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
	.chip {
		background: var(--panel-2);
		border: 1px solid color-mix(in srgb, var(--ok) 45%, var(--line));
		border-radius: var(--radius-sm);
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--amber) 55%, var(--line));
	}
	.row {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-end;
		gap: 8px;
	}
	input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 5px 9px;
		font-size: 12px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.btn.destroy {
		color: var(--fail);
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	.idcell {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.whlink {
		color: var(--ink);
		text-decoration: none;
	}
	.whlink:hover {
		text-decoration: underline;
	}
	/* drawer record + jump links */
	.chip.gold {
		border-color: color-mix(in srgb, var(--accent, #ffc14d) 55%, var(--line));
	}
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
</style>
