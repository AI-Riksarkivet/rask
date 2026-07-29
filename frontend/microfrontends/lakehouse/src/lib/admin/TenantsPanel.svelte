<script lang="ts">
	// `/tenants` — the estate's tenants on the shared @rask/ui DataTable (goal cond 4): one
	// sortable/searchable row per warehouse with its owning project + the project's effective FGA
	// admins (estate-admin gated BY THE CATALOG — the BFF only bearer-forwards). Read-only
	// observability: creation stays implicit via the warehouse-bind flow.
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
	import { Building2, ExternalLink, RefreshCw, ScrollText, ShieldAlert } from '@lucide/svelte';
	import { parse } from '@rask/api';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { ProjectsResponseSchema, type Project } from './tenants';
	import { requestJSON } from '$lib/http';
	import { controlCursor } from '$lib/live/feeds.remote';
	import { liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let projects = $state<Project[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let inflight = 0;

	const unauthorized = $derived(projects === null && settled && lastStatus === 401);
	const forbidden = $derived(projects === null && settled && lastStatus === 403);
	const drifted = $derived(projects === null && settled && lastStatus === -1);
	const offline = $derived(
		projects === null && settled && ![-1, 200, 401, 403].includes(lastStatus),
	);

	async function load(): Promise<void> {
		const seq = ++inflight;
		const res = await requestJSON<unknown>('/api', 'projects');
		if (seq !== inflight) return; // latest-wins
		settled = true;
		if (res.ok) {
			try {
				projects = parse(ProjectsResponseSchema, res.data);
				lastStatus = 200;
			} catch (err) {
				console.error(`tenants parse failure: ${String(err)}`);
				projects = null;
				lastStatus = -1;
			}
		} else {
			projects = null;
			lastStatus = res.status;
		}
	}

	// Live on the CONTROL cursor — one request in its life before this. Projects, warehouses and the
	// effective FGA admins on them are pure control plane: none of it writes a dataset, so none of it
	// moves the lineage cursor, and the catalog's own change feed is the only thing that reports it.
	liveRead(controlCursor, () => load());

	// One row per WAREHOUSE (project repeated) — flat rows sort/filter honestly; a project with no
	// warehouses still surfaces as a single warehouse-less row.
	type Row = {
		project: string;
		warehouse: string | null;
		bucket: string | null;
		status: string | null;
		admins: string[];
	};
	const rows: Row[] = $derived(
		(projects ?? []).flatMap((p): Row[] =>
			p.warehouses.length === 0
				? [{ project: p.project, warehouse: null, bucket: null, status: null, admins: p.admins }]
				: p.warehouses.map((w) => ({
						project: p.project,
						warehouse: w.id,
						bucket: w.bucket,
						status: w.status,
						admins: p.admins,
					})),
		),
	);

	// ── the row drawer (goal cond 8): click a tenant row → the full record + linked context. ──
	let drawerRow = $state<Row | null>(null);

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

	const columns: ColumnDef<Row>[] = [
		{
			id: 'project',
			accessorKey: 'project',
			header: sortableHeader('project'),
			meta: { cellClass: 'font-mono font-semibold' },
		},
		{
			id: 'warehouse',
			accessorFn: (r) => r.warehouse ?? '',
			header: sortableHeader('warehouse'),
			cell: ({ row }) => renderSnippet(warehouseCell, row.original),
		},
		{
			id: 'bucket',
			accessorFn: (r) => r.bucket ?? '',
			header: sortableHeader('bucket'),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'status',
			accessorFn: (r) => r.status ?? '',
			header: sortableHeader('status'),
			cell: ({ row }) => renderSnippet(statusCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'admins',
			accessorFn: (r) => r.admins.join(' '),
			header: 'admins',
			cell: ({ row }) => renderSnippet(adminsCell, row.original),
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

{#snippet warehouseCell(row: Row)}
	{#if row.warehouse}<span class="mono">{row.warehouse}</span>{:else}<span class="mut">(no
	warehouses)</span>{/if}
{/snippet}
{#snippet statusCell(row: Row)}
	{#if row.status}<span class="mono" class:warn={row.status !== 'active'}
	  >{row.status}</span
	>{:else}<span class="mut">—</span>{/if}
{/snippet}
{#snippet adminsCell(row: Row)}
	{#if row.admins.length === 0}
		<span class="mut">(none listed — FGA off or unavailable)</span>
	{:else}
		{#each row.admins as a (a)}<span class="chip mono">{a}</span>{/each}
	{/if}
{/snippet}

<div class="page">
	<header>
		<Building2 size={16} />
		<h1>Tenants</h1>
		<span class="sub mono">projects · warehouses · admins — derived from the registry + FGA</span>
	</header>

	<div class="bar">
		<button class="btn" onclick={load}><RefreshCw size={13} /> Refresh</button>
		<!-- The catalog is an AREA of this zone now, not a separate one: these are soft navs, and
		     forcing a document reload on them would throw away the merge's whole payoff. -->
		<a class="jump" href="{base}/data/warehouses">warehouses ↗</a>
		<a class="jump" href="{base}/data/namespaces">namespaces ↗</a>
	</div>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={15} /> <a href={loginHref} data-sveltekit-reload>Sign in</a> to view tenants.
		</div>
	{:else if forbidden}
		<div class="empty">
			<ShieldAlert size={15} /> Tenant enumeration is estate-admin only.
		</div>
	{:else if drifted}
		<div class="empty">
			<ShieldAlert size={15} /> The projects payload drifted from the contract — refusing to render.
		</div>
	{:else if offline}
		<div class="empty"><RefreshCw size={15} /> Catalog unreachable (HTTP {lastStatus}).</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Search tenants…" />
		</div>
		<DataTable
			{table}
			loading={projects === null}
			onrowclick={(r) => (drawerRow = r)}
			emptyMessage="No projects yet — the first warehouse-create brings its project into existence."
		/>
	{/if}
</div>

<Sheet.Root
	open={drawerRow !== null}
	onOpenChange={(o) => {
	if (!o) drawerRow = null;
}}
>
	<Sheet.Content side="right">
		{#if drawerRow}
			<Sheet.Header>
				<Sheet.Title>
					{drawerRow.warehouse
						? `Warehouse ${drawerRow.warehouse}`
						: `Project ${drawerRow.project}`}
				</Sheet.Title>
				<Sheet.Description>
					One tenant row off the registry + FGA — the project, its storage binding, and who administers
					it.
				</Sheet.Description>
			</Sheet.Header>
			<div class="drawer-body">
				<dl class="record">
					<dt>project</dt>
					<dd class="mono">{drawerRow.project}</dd>
					<dt>warehouse</dt>
					<dd class="mono">{drawerRow.warehouse ?? '(no warehouses)'}</dd>
					<dt>bucket</dt>
					<dd class="mono">{drawerRow.bucket ?? '—'}</dd>
					<dt>status</dt>
					<dd class="mono" class:warn={drawerRow.status !== null && drawerRow.status !== 'active'}>
						{drawerRow.status ?? '—'}
					</dd>
					<dt>admins</dt>
					<dd>
						{#if drawerRow.admins.length === 0}
							<span class="mut">(none listed — FGA off or unavailable)</span>
						{:else}
							{#each drawerRow.admins as a (a)}<span class="chip mono">{a}</span>{/each}
						{/if}
					</dd>
				</dl>
				<div class="drawer-links">
					{#if drawerRow.warehouse}
						<!-- Same-zone filtered view of the related audit events (the viewer reads ?resource=). -->
						<a
							class="btn jumplink"
							href={`${base}/admin/audit?resource=${encodeURIComponent(drawerRow.warehouse)}`}
						>
							<ScrollText size={12} /> Audit events for this warehouse
						</a>
					{/if}
					<a
						class="btn jumplink"
						href={`${base}/admin/audit?resource=${encodeURIComponent(drawerRow.project)}`}
					>
						<ScrollText size={12} /> Audit events for this project
					</a>
					<!-- The warehouse admin page lives in the catalog AREA of this same zone — soft nav. -->
					<a class="btn jumplink" href="{base}/data/warehouses">
						<ExternalLink size={12} /> Open warehouse admin ↗
					</a>
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
		gap: 8px;
		margin-bottom: 14px;
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
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		font-size: 13px;
		padding: 30px 0;
	}
	.warn {
		color: var(--warn, #d18b28);
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 1px 8px;
		color: var(--ink);
		margin-right: 4px;
	}
	.mut {
		color: var(--mut);
	}
	.jump {
		margin-left: auto;
		color: var(--mut);
		text-decoration: none;
		font-size: 12px;
	}
	.jump + .jump {
		margin-left: 0;
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
