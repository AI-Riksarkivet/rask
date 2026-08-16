<script lang="ts">
	// `/warehouses` — the #3-A control plane (#52 UI): provisioned warehouses (bucket-per-warehouse
	// physical tenancy), activate/deactivate lifecycle, namespace binding, and provisioning. Reads
	// are session-forwarded; writes are project-admin gated by the catalog (can_create_warehouse /
	// can_administer) — a non-admin sees the denial banner, never a silent no-op.
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
	import { Select } from '@rask/ui/select';
	import { RefreshCw, ShieldAlert, Warehouse as WarehouseIcon } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import type { Warehouse, WarehouseRecord } from './catalog';
	import {
		bindWarehouseNamespace,
		createWarehouse,
		fetchWarehouses,
		setWarehouseActive,
	} from './remote/warehouses.remote';
	import RowDrawer from './RowDrawer.svelte';
	import WarehouseDeleteDialog from './WarehouseDeleteDialog.svelte';
	import { controlCursor } from '$lib/live/feeds.remote';
	import { liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let warehouses = $state<WarehouseRecord[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let busy = $state(false);
	let banner = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);
	let draft = $state({ id: '', project: '', bucket: '' });
	let bindDraft = $state<{ warehouse: string; namespace: string }>({
		warehouse: '',
		namespace: '',
	});

	const unauthorized = $derived(warehouses === null && lastStatus === 401);
	const offline = $derived(warehouses === null && settled && lastStatus !== 401);

	// A pre-lifecycle warehouse (provisioned before the activate/deactivate feature) has no `status`
	// field — treat that as active, so it renders active with the correct toggle direction.
	function statusOf(w: Warehouse): string {
		return w.status ?? 'active';
	}

	async function load(): Promise<void> {
		const res = await fetchWarehouses();
		settled = true;
		if (res.ok) {
			warehouses = res.data;
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	// The CONTROL cursor, not the lineage one, and this is the surface that shows why the distinction is
	// real: provisioning a warehouse, binding a namespace or deactivating a bucket writes no dataset and
	// therefore emits no lineage event — the lineage cursor would never move for any of it. Warehouse
	// lifecycle is a catalog control-plane change, so that is the feed that reports it.
	//
	// The control feed is estate-admin gated (`can_observe_events` on the FGA root) while these READS are
	// open to any signed-in user, so a non-admin simply never ticks: they get the initial read plus the
	// re-read every mutation below already performs. That is the honest degradation — a non-admin cannot
	// change any of this, and the writes are project-admin gated anyway.
	liveRead(controlCursor, () => load());

	function fail(status: number, detail: string): void {
		if (status === 401)
			banner = { tone: 'fail', text: 'Sign in — warehouse admin is a per-user action.' };
		else if (status === 403 || status === 404)
			// The lifecycle ops hide existence from non-admins (a denied deactivate is a 404, not a 403),
			// so both map to the same admin-required message.
			banner = { tone: 'fail', text: 'Denied: warehouse admin needs the project-admin rung.' };
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

	// Goal cond 8: row click opens the full-record drawer (link + lifecycle button stopPropagation).
	let drawerOpen = $state(false);
	let drawerRow = $state<WarehouseRecord | null>(null);
	function openDrawer(row: WarehouseRecord): void {
		drawerRow = row;
		drawerOpen = true;
	}

	// The destroy door. It gets its own dialog rather than a banner-confirm because its three opt-ins
	// (cascade / purge_bucket / force) each need to be argued for separately — see
	// `WarehouseDeleteDialog`. The list re-reads on `ondeleted` for immediacy; the catalog also emits a
	// `warehouse_deleted` control event, so the `liveRead(controlCursor, …)` above re-reads every OTHER
	// open warehouse list on the estate too.
	let deleteOpen = $state(false);
	let deleteRow = $state<WarehouseRecord | null>(null);
	function openDelete(row: WarehouseRecord): void {
		deleteRow = row;
		deleteOpen = true;
	}

	const columns: ColumnDef<WarehouseRecord>[] = [
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
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'bucket',
			accessorKey: 'bucket',
			header: sortableHeader('bucket'),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'status',
			accessorFn: (w) => statusOf(w),
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

	const whTable = createSvelteTable({
		get data() {
			return warehouses ?? [];
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

	async function bind(): Promise<void> {
		if (busy || !bindDraft.warehouse || !bindDraft.namespace.trim()) return;
		busy = true;
		banner = null;
		try {
			const res = await bindWarehouseNamespace({
				id: bindDraft.warehouse,
				namespace: bindDraft.namespace.trim(),
			});
			if (res.ok) {
				banner = {
					tone: 'ok',
					text: `namespace ${bindDraft.namespace} bound to ${bindDraft.warehouse}`,
				};
				bindDraft = { warehouse: '', namespace: '' };
			} else {
				fail(res.status, res.detail);
			}
		} finally {
			busy = false;
		}
	}
</script>

{#snippet idCell(w: Warehouse)}
	<a
		class="whlink mono"
		href={`${base}/catalog/warehouses/${encodeURIComponent(w.id)}`}
		onclick={(e) => e.stopPropagation()}>{w.id}</a
	>
{/snippet}
{#snippet statusCell(w: Warehouse)}
	<span class="chip mono" class:off={statusOf(w) !== 'active'}>{statusOf(w)}</span>
{/snippet}
{#snippet actionsCell(w: WarehouseRecord)}
	<button
		class="btn ghost"
		disabled={busy}
		onclick={(e) => {
	e.stopPropagation();
	toggleActive(w);
}}
	>
		{statusOf(w) === 'active' ? 'deactivate' : 'activate'}
	</button>
	<button
		class="btn ghost destroy"
		disabled={busy}
		aria-label={`Delete warehouse ${w.id}`}
		onclick={(e) => {
	e.stopPropagation();
	openDelete(w);
}}
	>
		delete
	</button>
{/snippet}

<div class="page">
	<header>
		<WarehouseIcon size={16} />
		<h1>Warehouses</h1>
		<span class="sub mono">every warehouse your grants allow, across projects · bucket-per-warehouse tenancy · project-admin gated</span>
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
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view warehouses.
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
		</div>
		<DataTable
			table={whTable}
			loading={warehouses === null}
			emptyMessage="No warehouses provisioned — the form below creates the first."
			onrowclick={openDrawer}
		/>

		<section>
			<h2>Provision</h2>
			<form
				class="row"
				onsubmit={(e) => {
	e.preventDefault();
	provision();
}}
			>
				<input
					class="mono"
					bind:value={draft.id}
					placeholder="warehouse id"
					aria-label="Warehouse id"
				/>
				<input class="mono" bind:value={draft.project} placeholder="project" aria-label="Project" />
				<input
					class="mono"
					bind:value={draft.bucket}
					placeholder="bucket (defaults to id)"
					aria-label="Bucket"
				/>
				<button
					class="btn"
					type="submit"
					disabled={busy || !draft.id.trim() || !draft.project.trim()}
				>
					Provision
				</button>
			</form>
		</section>

		<section>
			<h2>Bind namespace</h2>
			<form
				class="row"
				onsubmit={(e) => {
	e.preventDefault();
	bind();
}}
			>
				<Select
					bind:value={bindDraft.warehouse}
					ariaLabel="Warehouse"
					placeholder="warehouse…"
					options={(warehouses ?? []).map((w) => ({ value: w.id, label: w.id }))}
				/>
				<input
					class="mono"
					bind:value={bindDraft.namespace}
					placeholder="top-level namespace"
					aria-label="Namespace"
				/>
				<button
					class="btn"
					type="submit"
					disabled={busy || !bindDraft.warehouse || !bindDraft.namespace.trim()}
				>
					Bind
				</button>
			</form>
			<p class="mut">
				Binding routes a top-level namespace's tables to the warehouse's bucket (immutable once set).
			</p>
		</section>
	{/if}
</div>

<RowDrawer
	bind:open={drawerOpen}
	title={drawerRow?.id ?? ''}
	description="The full warehouse registry record."
>
	{#if drawerRow}
		<dl class="rec">
			<dt>warehouse id</dt>
			<dd class="mono">{drawerRow.id}</dd>
			<dt>project</dt>
			<dd>
				<!-- The project rung is the TOP of the hierarchy and lives in the HOME zone (2026-08-03
				     ruling); this zone owns warehouse and below. So the href is absolute rather than
				     base-relative, and it MUST hard-navigate — a soft nav would land in a route this zone
				     does not own. -->
				<a
					class="mono jump"
					href={`/projects/${encodeURIComponent(drawerRow.project)}`}
					data-sveltekit-reload>{drawerRow.project}</a
				>
			</dd>
			<dt>bucket</dt>
			<dd class="mono">{drawerRow.bucket}</dd>
			<dt>root uri</dt>
			<dd class="mono">{drawerRow.root_uri}</dd>
			<dt>status</dt>
			<dd>
				<span class="chip mono" class:off={statusOf(drawerRow) !== 'active'}>{statusOf(drawerRow)}</span
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
		</dl>
		<div class="jumps">
			<a class="jbtn" href={`${base}/catalog/warehouses/${encodeURIComponent(drawerRow.id)}`}
				>Open detail</a
			>
			<!-- cross-zone, same reason as the project cell above -->
			<a
				class="jbtn"
				href={`/projects/${encodeURIComponent(drawerRow.project)}`}
				data-sveltekit-reload>Open project</a
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
