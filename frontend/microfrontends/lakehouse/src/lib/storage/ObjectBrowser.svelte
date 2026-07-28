<script lang="ts">
	// `/storage` — the R18 S3 object browser over the warehouse buckets, on the shared @rask/ui
	// DataTable: one delimiter-scoped level per view (folders + leaf objects), sortable columns, a
	// text search, breadcrumb prefix navigation and a preview pane beside the table. Backend is
	// the media-plane viewer's objects endpoints through this zone's /api/media BFF route
	// (volumes-api retired in the R6/R20 wave); unreachable and empty states
	// are explicit — a dead backend renders a retryable message, never a stuck spinner.
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
	import { File, Folder, RefreshCw } from '@lucide/svelte';
	import { untrack } from 'svelte';
	import ObjectPreview from './ObjectPreview.svelte';
	import PagePreview from './PagePreview.svelte';
	import {
		fmtModified,
		fmtSize,
		listObjects,
		listStores,
		looksLikeLanceDataset,
		tableIdForPrefix,
		type Bucket,
		type S3Listing,
		type Store,
	} from './storage';

	// The stores come from the catalog's registry (R28), not a hardcoded const. Empty until the
	// first read lands, so the picker renders whatever the estate actually registered — including
	// stores this build has never heard of.
	let stores = $state<Store[]>([]);
	let bucket = $state<Bucket>('');
	let prefix = $state('');
	let listing = $state<S3Listing | null>(null);
	let lastStatus = $state(0);
	// The server's own problem+json `detail`. A missing bucket is a 404 that NAMES the bucket and the
	// values key that provisions it (live-proof 2026-07-28 defect 2); rendering our own generic
	// "unreachable" over it threw away the only sentence that said what was actually wrong.
	let lastDetail = $state('');
	let settled = $state(false); // distinguishes "still loading" (unsettled) from a failure (settled)
	let selected = $state<Row | null>(null);

	const loading = $derived(listing === null && !settled);
	const offline = $derived(listing === null && settled);

	/** Pull the registry, then land on the first store. R28: which stores exist is the CATALOG's
	 *  answer, so an estate that registers a new bucket gets it here without a frontend release. */
	async function loadStores(): Promise<void> {
		const res = await listStores();
		if (!res.ok) return;
		stores = res.data.stores;
		if (!bucket && stores.length) bucket = stores[0]!.name;
	}

	async function load(): Promise<void> {
		// Nothing to list until the registry says which stores exist.
		if (!bucket) return;
		// Latest-wins: a slow response for a level the user already left must not clobber the view.
		const wantBucket = bucket;
		const wantPrefix = prefix;
		listing = null;
		settled = false;
		const res = await listObjects(wantBucket, wantPrefix);
		if (bucket !== wantBucket || prefix !== wantPrefix) return;
		settled = true;
		if (res.ok) {
			listing = res.data;
			lastStatus = 200;
			lastDetail = '';
		} else {
			lastStatus = res.status; // status 0 (offline/timeout) reads as unreachable, not a spinner
			lastDetail = res.detail;
		}
	}

	$effect(() => {
		void loadStores();
	});

	$effect(() => {
		// Reset + reload on bucket/prefix change; untracked so load()'s own reads don't retrigger.
		void bucket;
		void prefix;
		selected = null;
		untrack(() => load());
	});

	/** Bucket switch via a function binding: a new bucket starts at ITS root, never at a prefix that
	 *  only existed in the previous one. */
	function setBucket(next: string): void {
		const found = stores.find((s) => s.name === next);
		if (!found || found.name === bucket) return;
		bucket = found.name;
		prefix = '';
	}

	// A Lance dataset is a DIRECTORY of objects, so the browser can walk into one. Detect that and
	// swap the byte-preview for the page viewer — same panel, read as what the location actually is.
	const isLanceDataset = $derived(
		listing !== null && looksLikeLanceDataset(listing.prefixes, listing.objects),
	);
	// The CATALOG TABLE the viewer reads — `<namespace>$<table>`, derived from the prefix, because
	// tiers are namespaces and a registered table lives at `<namespace>/<table>/`. Null for a dataset
	// sitting anywhere else: that is real storage but not a registered table, and the viewer reads
	// through the catalog rather than round it.
	const datasetTable = $derived(tableIdForPrefix(prefix));

	const crumbs = $derived.by(() => {
		const parts = prefix.split('/').filter(Boolean);
		return parts.map((label, i) => ({ label, prefix: `${parts.slice(0, i + 1).join('/')}/` }));
	});

	// ── the DataTable ──
	// One uniform row shape for folders + objects (folder size −1 so an ascending size sort keeps
	// folders first); `id` is the full prefix/key, `name` the segment relative to the current level.
	type Row = {
		kind: 'folder' | 'object';
		id: string;
		name: string;
		size: number;
		modified: string | null;
	};
	const rows = $derived.by((): Row[] => {
		const l = listing;
		if (l === null) return [];
		const folders = l.prefixes.map(
			(p): Row => ({
				kind: 'folder',
				id: p,
				name: p.slice(l.prefix.length),
				size: -1,
				modified: null,
			}),
		);
		const objects = l.objects.map(
			(o): Row => ({
				kind: 'object',
				id: o.key,
				name: o.key.slice(l.prefix.length),
				size: o.size,
				modified: o.last_modified,
			}),
		);
		return [...folders, ...objects];
	});

	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });

	function openRow(row: Row): void {
		if (row.kind === 'folder') {
			prefix = row.id;
		} else {
			selected = row;
		}
	}

	const columns: ColumnDef<Row>[] = [
		{
			id: 'name',
			accessorKey: 'name',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'name',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nameCell, row.original),
		},
		{
			id: 'size',
			accessorKey: 'size',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'size',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(sizeCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'modified',
			accessorKey: 'modified',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'modified',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(modifiedCell, row.original),
			meta: { headerClass: 'w-44' },
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

{#snippet nameCell(row: Row)}
	<span class="name mono" class:folder={row.kind === 'folder'}>
		{#if row.kind === 'folder'}<Folder size={13} />{:else}<File size={13} />{/if}
		{row.name}
	</span>
{/snippet}
{#snippet sizeCell(row: Row)}
	{#if row.kind === 'folder'}<span class="mut">—</span>{:else}<span class="mono">{fmtSize(row.size)}</span>{/if}
{/snippet}
{#snippet modifiedCell(row: Row)}
	{#if row.modified === null}<span class="mut">—</span>{:else}<span class="mono">{fmtModified(row.modified)}</span>{/if}
{/snippet}

<div class="page">
	<header>
		<h1>Objects</h1>
		<span class="sub mono">the warehouse buckets · s3://{bucket}/{prefix}</span>
	</header>

	<div class="toolbar">
		<Select
			bind:value={() => bucket, setBucket}
			options={stores.map((s) => ({ value: s.name, label: `${s.name} · ${s.role}` }))}
			ariaLabel="Store"
		/>
		<DataTableTextFilter bind:value={globalFilter} placeholder="Search objects…" />
		<button class="btn refresh" onclick={load} disabled={loading}>
			<RefreshCw size={12} /> Refresh
		</button>
	</div>

	<nav class="crumbs mono" aria-label="Prefix breadcrumb">
		<button class="crumb" onclick={() => (prefix = '')} disabled={prefix === ''}>{bucket}</button>
		{#each crumbs as c (c.prefix)}
			<span class="sep">/</span>
			<button class="crumb" onclick={() => (prefix = c.prefix)} disabled={c.prefix === prefix}>
				{c.label}
			</button>
		{/each}
	</nav>

	{#if offline}
		<div class="empty">
			<RefreshCw size={16} />
			{#if lastStatus === 404 && lastDetail !== ''}
				<!-- A diagnosable, expected state (usually: the bucket was never provisioned). Show what
				     the backend said — it names the bucket and the values key — instead of "unreachable",
				     which is both wrong about the layer and useless for fixing it. -->
				<p>{lastDetail}</p>
			{:else}
				<p>Storage service unreachable (HTTP {lastStatus}).</p>
			{/if}
			<button class="btn" onclick={load}>Retry</button>
		</div>
	{:else}
		<div class="split" class:withpane={selected !== null}>
			<div class="tablecol">
				<DataTable
					{table}
					{loading}
					emptyMessage="No objects under this prefix — the bucket is empty here."
					onrowclick={openRow}
				/>
			</div>
			{#if isLanceDataset && datasetTable}
				<!-- Walking into a Lance dataset with the plain object browser shows `.lance` fragments
				     and a `_versions` manifest — the storage layout, not the document. When the current
				     prefix IS a dataset, the panel reads it as rows with a blob-v2 image column instead. -->
				<PagePreview table={datasetTable} />
			{:else if selected !== null}
				<ObjectPreview {bucket} key={selected.id} onclose={() => (selected = null)} />
			{/if}
		</div>
	{/if}
</div>

<style>
	.page {
		max-width: 1160px;
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
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
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
	.refresh {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		margin-left: auto;
	}
	.crumbs {
		display: flex;
		align-items: center;
		gap: 2px;
		margin-bottom: 10px;
		font-size: 12px;
		flex-wrap: wrap;
	}
	.crumb {
		background: none;
		border: none;
		color: var(--mut);
		cursor: pointer;
		padding: 2px 4px;
		font-size: 12px;
	}
	.crumb:hover:enabled {
		color: var(--ink);
		text-decoration: underline;
	}
	.crumb:disabled {
		color: var(--ink);
		cursor: default;
	}
	.sep {
		color: var(--faint);
	}
	.split {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 16px;
		align-items: start;
	}
	.split.withpane {
		grid-template-columns: minmax(0, 1fr) 340px;
	}
	@media (max-width: 900px) {
		.split.withpane {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	.tablecol {
		min-width: 0;
	}
	.name {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
	}
	.name.folder {
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
	.empty p {
		margin: 0;
	}
</style>
