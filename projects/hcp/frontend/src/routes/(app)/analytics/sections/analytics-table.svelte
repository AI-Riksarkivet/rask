<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Slider } from '$lib/components/ui/slider/index.js';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import FileViewer from '$lib/components/custom/file-viewer/FileViewer.svelte';
	import { get_lance_rows, get_lance_schema, search_lance } from '$lib/remote/lance.remote.js';
	import {
		cellUrl,
		type LanceField,
		type VectorValue,
		type BinaryCellMeta,
	} from '$lib/types/lance.js';
	import { Search, SlidersHorizontal, Radar, X } from 'lucide-svelte';
	import {
		DataTable,
		createSvelteTable,
		getCoreRowModel,
		renderSnippet,
	} from '$lib/components/ui/data-table/index.js';
	import type { ColumnDef, PaginationState, VisibilityState } from '@tanstack/table-core';
	import { formatBytes } from '$lib/utils/format.js';
	import { onDestroy } from 'svelte';

	let {
		bucket,
		path,
		table: tableName,
	}: { bucket: string; path: string; table: string } = $props();

	const PAGE_SIZE = 50;

	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: PAGE_SIZE });
	let columnVisibility = $state<VisibilityState>({});
	let filterInput = $state('');
	let activeFilter = $state('');

	// Search state
	let searchInput = $state('');
	let searchType = $state<'fts' | 'vector' | 'hybrid'>('fts');
	let selectedVectorCol = $state('');
	let searchResults = $state<Record<string, unknown>[]>([]);
	let searchTotal = $state(0);
	let searchError = $state<string | null>(null);
	let searchActive = $state(false);
	let hybridWeight = $state([70]);

	// Timer cleanup for search polling
	let activeIntervals: ReturnType<typeof setInterval>[] = [];
	let activeTimeouts: ReturnType<typeof setTimeout>[] = [];

	function clearActiveTimers() {
		activeIntervals.forEach(clearInterval);
		activeTimeouts.forEach(clearTimeout);
		activeIntervals = [];
		activeTimeouts = [];
	}

	onDestroy(clearActiveTimers);

	// Row detail dialog state
	let detailOpen = $state(false);
	let detailRow = $state<Record<string, unknown> | null>(null);
	let detailRowIndex = $state<number>(0);

	// FileViewer state for binary cell preview
	let fileViewerOpen = $state(false);
	let fileViewerUrl = $state('');
	let fileViewerFilename = $state('');
	let fileViewerSize = $state<number | undefined>(undefined);

	// Reset everything when the selected table changes.
	let resetTable = (() => {
		let prev = '';
		return (current: string) => {
			if (prev && prev !== current) {
				pagination = { pageIndex: 0, pageSize: PAGE_SIZE };
				columnVisibility = {};
				filterInput = '';
				activeFilter = '';
				searchInput = '';
				searchResults = [];
				searchTotal = 0;
				searchError = null;
				searchActive = false;
				selectedVectorCol = '';
			}
			prev = current;
		};
	})();
	$effect.pre(() => {
		resetTable(tableName);
	});

	let offset = $derived(pagination.pageIndex * pagination.pageSize);

	let schemaData = $derived(
		get_lance_schema({ bucket, path: path || undefined, table: tableName })
	);
	let fields = $derived((schemaData?.current?.fields ?? []) as LanceField[]);
	let vectorFields = $derived(fields.filter((f) => f.is_vector));
	let activeVectorCol = $derived(selectedVectorCol || vectorFields[0]?.name || '');
	let hasTextFields = $derived(
		fields.some((f) => !f.is_vector && !f.is_binary && f.type === 'string')
	);

	let rowsData = $derived(
		get_lance_rows({
			bucket,
			path: path || undefined,
			table: tableName,
			limit: pagination.pageSize,
			offset,
			filter: activeFilter || undefined,
		})
	);
	let rows = $derived((rowsData?.current?.rows ?? []) as Record<string, unknown>[]);
	let total = $derived((rowsData?.current?.total ?? 0) as number);
	let error = $derived(rowsData?.current?.error ?? null);

	// Which data to display depends on whether search is active
	let displayRows = $derived(searchActive ? searchResults : rows);
	let displayTotal = $derived(searchActive ? searchTotal : total);
	let displayError = $derived(searchActive ? searchError : error);

	let hiddenCount = $derived(Object.values(columnVisibility).filter((v) => v === false).length);

	function applyFilter() {
		activeFilter = filterInput.trim();
		pagination = { pageIndex: 0, pageSize: pagination.pageSize };
	}

	function clearSearch() {
		searchActive = false;
		searchResults = [];
		searchTotal = 0;
		searchError = null;
		searchInput = '';
	}

	async function executeSearch() {
		const q = searchInput.trim();
		if (!q) return;

		clearActiveTimers();
		searchError = null;
		searchActive = true;

		const searchData = search_lance({
			bucket,
			path: path || undefined,
			table: tableName,
			query: searchType !== 'vector' ? q : undefined,
			query_type: searchType,
			vector_column: activeVectorCol || undefined,
			limit: 20,
			weight:
				searchType === 'hybrid' && Number.isFinite(hybridWeight[0])
					? hybridWeight[0] / 100
					: undefined,
		});

		// The remote function is reactive -- poll until result arrives
		const interval = setInterval(() => {
			const result = searchData?.current;
			if (result) {
				searchResults = (result.rows ?? []) as Record<string, unknown>[];
				searchTotal = (result.total ?? 0) as number;
				searchError = (result.error as string) ?? null;
				clearInterval(interval);
			}
		}, 200);
		activeIntervals.push(interval);
		// Safety: stop polling after 10s
		activeTimeouts.push(setTimeout(() => clearInterval(interval), 10000));
	}

	function findSimilar(row: Record<string, unknown>) {
		if (vectorFields.length === 0) return;
		const colName = activeVectorCol;
		const vecVal = row[colName] as VectorValue | null;
		if (!vecVal?.preview) return;

		clearActiveTimers();
		searchType = 'vector';
		searchInput = `Similar to row (vector: ${colName})`;
		searchError = null;
		searchActive = true;

		const searchData = search_lance({
			bucket,
			path: path || undefined,
			table: tableName,
			vector: JSON.stringify(vecVal.preview),
			vector_column: colName,
			query_type: 'vector',
			limit: 20,
		});

		const interval = setInterval(() => {
			const result = searchData?.current;
			if (result) {
				searchResults = (result.rows ?? []) as Record<string, unknown>[];
				searchTotal = (result.total ?? 0) as number;
				searchError = (result.error as string) ?? null;
				clearInterval(interval);
			}
		}, 200);
		activeIntervals.push(interval);
		activeTimeouts.push(setTimeout(() => clearInterval(interval), 10000));
	}

	function openDetail(row: Record<string, unknown>) {
		detailRow = row;
		// Find the row's position in the currently displayed data.
		const idx = displayRows.indexOf(row);
		// For browse mode, absolute index = offset + position in page.
		// For search mode, the position within search results is the index itself.
		detailRowIndex = searchActive ? idx : offset + idx;
		detailOpen = true;
	}

	function openFileViewer(column: string, rowIndex: number, binVal: BinaryCellMeta) {
		fileViewerUrl = cellUrl(bucket, tableName, column, rowIndex, path || undefined);
		fileViewerFilename = `${column}_row${rowIndex}`;
		fileViewerSize = binVal.size ?? undefined;
		fileViewerOpen = true;
	}

	function formatDetailValue(val: unknown): string {
		if (val == null) return 'null';
		if (typeof val === 'object') return JSON.stringify(val, null, 2);
		return String(val);
	}

	function makeColumns(addDistance: boolean): ColumnDef<Record<string, unknown>>[] {
		const cols: ColumnDef<Record<string, unknown>>[] = [];

		if (addDistance) {
			cols.push({
				id: '_distance',
				accessorKey: '_distance',
				header: 'Score',
				cell: ({ row }) => {
					const val = row.original._distance as number | undefined;
					return val != null ? val.toFixed(4) : '';
				},
				meta: { cellClass: 'px-4 py-3 font-mono text-xs' },
			});
		}

		for (const field of fields) {
			if (field.is_binary) {
				cols.push({
					id: field.name,
					accessorKey: field.name,
					header: field.name,
					cell: ({ row }) => {
						const val = row.original[field.name] as BinaryCellMeta | null;
						const rowIndex = searchActive ? row.index : offset + row.index;
						return renderSnippet(binaryCell, {
							val,
							column: field.name,
							rowIndex,
						});
					},
					meta: { cellClass: 'px-4 py-3' },
				});
			} else if (field.is_vector) {
				cols.push({
					id: field.name,
					accessorKey: field.name,
					header: field.name,
					cell: ({ row }) => {
						const val = row.original[field.name] as VectorValue | null;
						return renderSnippet(vectorCell, { val });
					},
					meta: { cellClass: 'px-4 py-3' },
				});
			} else {
				cols.push({
					id: field.name,
					accessorKey: field.name,
					header: field.name,
					cell: ({ row }) => {
						const val = row.original[field.name];
						if (val == null) return '';
						if (typeof val === 'object') return JSON.stringify(val);
						return String(val);
					},
					meta: { cellClass: 'max-w-[300px] truncate px-4 py-3' },
				});
			}
		}
		return cols;
	}

	let columns = $derived.by(() => makeColumns(searchActive));

	let tanstackTable = $derived(
		createSvelteTable({
			get data() {
				return displayRows;
			},
			get columns() {
				return columns;
			},
			state: {
				get pagination() {
					return pagination;
				},
				get columnVisibility() {
					return columnVisibility;
				},
			},
			onPaginationChange: (updater) => {
				pagination = typeof updater === 'function' ? updater(pagination) : updater;
			},
			onColumnVisibilityChange: (updater) => {
				columnVisibility = typeof updater === 'function' ? updater(columnVisibility) : updater;
			},
			getCoreRowModel: getCoreRowModel(),
			manualPagination: true,
			get rowCount() {
				return displayTotal;
			},
		})
	);
</script>

{#snippet binaryCell(props: { val: BinaryCellMeta | null; column: string; rowIndex: number })}
	{#if props.val?.size}
		<img
			src={cellUrl(bucket, tableName, props.column, props.rowIndex, path || undefined)}
			alt={props.column}
			class="h-12 w-12 rounded object-cover"
			loading="lazy"
			onerror={(e) => {
				const target = e.currentTarget as HTMLImageElement;
				const span = document.createElement('span');
				span.className = 'text-xs text-muted-foreground';
				span.textContent = `${props.val?.size} bytes`;
				target.replaceWith(span);
			}}
		/>
	{:else}
		<span class="text-muted-foreground">null</span>
	{/if}
{/snippet}

{#snippet vectorCell(props: { val: VectorValue | null })}
	{#if props.val && props.val.type === 'vector'}
		<Badge variant="outline" class="font-mono text-xs">
			vec[{props.val.dim}] ‖{props.val.norm?.toFixed(2)}‖
		</Badge>
	{:else}
		<span class="text-muted-foreground">null</span>
	{/if}
{/snippet}

<Card.Root>
	<Card.Header>
		<div class="flex items-center justify-between">
			<Card.Title class="text-sm">
				Data — {tableName}
				{#if displayTotal > 0}
					<Badge variant="secondary" class="ml-2"
						>{displayTotal.toLocaleString()}
						{searchActive ? 'results' : 'rows'}</Badge
					>
				{/if}
			</Card.Title>
			<div class="flex items-center gap-2">
				{#if searchActive}
					<Button variant="outline" size="sm" onclick={clearSearch}>
						<X class="mr-1.5 h-3.5 w-3.5" />
						Clear search
					</Button>
				{/if}
				{#if fields.length > 0}
					<DropdownMenu.Root>
						<DropdownMenu.Trigger>
							{#snippet child({ props })}
								<Button variant="outline" size="sm" {...props}>
									<SlidersHorizontal class="mr-2 h-4 w-4" />
									Columns
									{#if hiddenCount > 0}
										<Badge variant="secondary" class="ml-1.5"
											>{fields.length - hiddenCount}/{fields.length}</Badge
										>
									{/if}
								</Button>
							{/snippet}
						</DropdownMenu.Trigger>
						<DropdownMenu.Content align="end" class="max-h-72 w-48 overflow-y-auto">
							{#each fields as field (field.name)}
								<DropdownMenu.CheckboxItem
									checked={columnVisibility[field.name] !== false}
									onCheckedChange={(checked) => {
										columnVisibility = { ...columnVisibility, [field.name]: checked };
									}}
								>
									<span class="truncate">{field.name}</span>
									{#if field.is_vector}
										<Badge variant="outline" class="ml-auto text-[10px]">vec</Badge>
									{:else if field.is_binary}
										<Badge variant="outline" class="ml-auto text-[10px]">bin</Badge>
									{/if}
								</DropdownMenu.CheckboxItem>
							{/each}
						</DropdownMenu.Content>
					</DropdownMenu.Root>
				{/if}
			</div>
		</div>
	</Card.Header>
	<Card.Content class="space-y-3">
		<!-- Search -->
		{#if hasTextFields || vectorFields.length > 0}
			<div class="space-y-2.5">
				<!-- Search mode tabs -->
				<div class="flex items-center gap-1.5">
					<span class="mr-1 text-xs font-medium text-muted-foreground">Search mode</span>
					{#if hasTextFields}
						<Button
							variant={searchType === 'fts' ? 'default' : 'outline'}
							size="sm"
							class="h-7 px-3 text-xs"
							onclick={() => {
								searchType = 'fts';
							}}
						>
							<Search class="mr-1.5 h-3 w-3" />
							Full-Text
						</Button>
					{/if}
					{#if vectorFields.length > 0}
						<Button
							variant={searchType === 'vector' ? 'default' : 'outline'}
							size="sm"
							class="h-7 px-3 text-xs"
							onclick={() => {
								searchType = 'vector';
							}}
						>
							<Radar class="mr-1.5 h-3 w-3" />
							Vector
						</Button>
					{/if}
					{#if hasTextFields && vectorFields.length > 0}
						<Button
							variant={searchType === 'hybrid' ? 'default' : 'outline'}
							size="sm"
							class="h-7 px-3 text-xs"
							onclick={() => {
								searchType = 'hybrid';
							}}
						>
							<Radar class="mr-1.5 h-3 w-3" />
							Hybrid
						</Button>
					{/if}

					<!-- Vector column selector (inline) -->
					{#if vectorFields.length > 1 && (searchType === 'vector' || searchType === 'hybrid')}
						<span class="ml-2 border-l pl-2 text-xs text-muted-foreground">on</span>
						{#each vectorFields as vf (vf.name)}
							<Button
								variant={activeVectorCol === vf.name ? 'secondary' : 'ghost'}
								size="sm"
								class="h-7 px-2 text-xs"
								onclick={() => {
									selectedVectorCol = vf.name;
								}}
							>
								{vf.name}
								{#if vf.vector_dim}
									<Badge variant="outline" class="ml-1 text-[9px]">{vf.vector_dim}</Badge>
								{/if}
							</Button>
						{/each}
					{/if}
				</div>

				<!-- Search input -->
				<form
					class="flex items-center gap-2"
					onsubmit={(e) => {
						e.preventDefault();
						executeSearch();
					}}
				>
					<div class="relative flex-1">
						<Search
							class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
						/>
						<Input
							bind:value={searchInput}
							placeholder={searchType === 'fts'
								? 'Search text across all string fields...'
								: searchType === 'hybrid'
									? 'Search with text + vector similarity...'
									: 'Describe what to search for...'}
							class="pl-10"
						/>
					</div>
					<Button type="submit">
						<Search class="mr-2 h-4 w-4" />
						Search
					</Button>
				</form>

				<!-- Hybrid weight slider -->
				{#if searchType === 'hybrid'}
					<div class="flex items-center gap-3 rounded-md border bg-muted/30 px-4 py-2">
						<span class="shrink-0 text-xs font-medium">Vector</span>
						<Slider
							type="single"
							bind:value={hybridWeight}
							min={0}
							max={100}
							step={5}
							class="flex-1"
						/>
						<span class="shrink-0 text-xs font-medium">FTS</span>
						<Badge variant="secondary" class="ml-1 font-mono text-xs tabular-nums"
							>{hybridWeight[0]}% FTS</Badge
						>
					</div>
				{/if}
			</div>
		{/if}

		<!-- SQL filter -->
		<form
			class="flex items-center gap-2"
			onsubmit={(e) => {
				e.preventDefault();
				applyFilter();
			}}
		>
			<div class="relative flex-1">
				<SlidersHorizontal
					class="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
				/>
				<Input
					bind:value={filterInput}
					placeholder="SQL filter — e.g. label = 'cat' AND score > 0.5"
					class="h-8 pl-9 text-xs"
				/>
			</div>
			<Button type="submit" variant="outline" size="sm" class="h-8">
				<SlidersHorizontal class="mr-1.5 h-3 w-3" />
				Filter
			</Button>
		</form>

		{#if searchActive && !searchError && searchResults.length === 0}
			<p class="text-sm text-muted-foreground">
				No results found. Try a different query or check that an FTS index exists.
			</p>
		{/if}

		{#if displayError}
			<ErrorBanner message={String(displayError)} />
		{:else if fields.length === 0}
			<TableSkeleton rows={5} columns={4} />
		{:else}
			<DataTable table={tanstackTable} onrowclick={openDetail} noResultsMessage="No rows found." />
		{/if}
	</Card.Content>
</Card.Root>

<!-- Row Detail Dialog -->
<Dialog.Root bind:open={detailOpen}>
	<Dialog.Content class="sm:max-w-2xl">
		<Dialog.Header>
			<div class="flex items-center justify-between">
				<div>
					<Dialog.Title>Row Detail</Dialog.Title>
					<Dialog.Description>All fields for the selected row</Dialog.Description>
				</div>
				{#if vectorFields.length > 0 && detailRow}
					<Button
						variant="outline"
						size="sm"
						onclick={() => {
							if (detailRow) {
								findSimilar(detailRow);
								detailOpen = false;
							}
						}}
					>
						<Radar class="mr-2 h-4 w-4" />
						Find Similar
					</Button>
				{/if}
			</div>
		</Dialog.Header>
		{#if detailRow}
			<div class="max-h-[60vh] overflow-y-auto">
				<div class="grid gap-3">
					{#each fields as field (field.name)}
						{@const val = detailRow[field.name]}
						<div class="rounded-md border p-3">
							<div class="mb-1 flex items-center gap-2">
								<span class="text-xs font-semibold">{field.name}</span>
								<span class="text-xs text-muted-foreground">{field.type}</span>
								{#if field.is_vector}
									<Badge variant="outline" class="text-[10px]">vector</Badge>
								{:else if field.is_binary}
									<Badge variant="outline" class="text-[10px]">binary</Badge>
								{/if}
							</div>
							{#if field.is_binary}
								{@const binVal = val as BinaryCellMeta | null}
								{#if binVal?.size}
									<button
										type="button"
										class="group relative cursor-pointer rounded border-0 bg-transparent p-0"
										onclick={() => {
											if (binVal) openFileViewer(field.name, detailRowIndex, binVal);
										}}
									>
										<img
											src={cellUrl(
												bucket,
												tableName,
												field.name,
												detailRowIndex,
												path || undefined
											)}
											alt={field.name}
											class="max-h-48 rounded object-contain transition-opacity group-hover:opacity-80"
										/>
										<span
											class="absolute inset-0 flex items-center justify-center rounded bg-black/40 text-xs font-medium text-white opacity-0 transition-opacity group-hover:opacity-100"
										>
											Click to preview ({formatBytes(binVal.size)})
										</span>
									</button>
								{:else}
									<span class="text-sm text-muted-foreground">null</span>
								{/if}
							{:else if field.is_vector}
								{@const vecVal = val as VectorValue | null}
								{#if vecVal && vecVal.type === 'vector'}
									<div class="space-y-1">
										<div class="flex flex-wrap gap-2 text-xs">
											<Badge variant="secondary">dim: {vecVal.dim}</Badge>
											<Badge variant="secondary">norm: {vecVal.norm?.toFixed(4)}</Badge>
											<Badge variant="secondary">min: {vecVal.min?.toFixed(4)}</Badge>
											<Badge variant="secondary">max: {vecVal.max?.toFixed(4)}</Badge>
											<Badge variant="secondary">mean: {vecVal.mean?.toFixed(4)}</Badge>
										</div>
										{#if vecVal.preview?.length}
											<div
												class="mt-2 rounded bg-muted p-2 font-mono text-xs leading-relaxed break-all"
											>
												[{vecVal.preview
													.slice(0, 16)
													.map((v) => v?.toFixed(4))
													.join(', ')}{vecVal.preview.length > 16 ? ', ...' : ''}]
											</div>
										{/if}
									</div>
								{:else}
									<span class="text-sm text-muted-foreground">null</span>
								{/if}
							{:else}
								<pre
									class="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 font-mono text-xs">{formatDetailValue(
										val
									)}</pre>
							{/if}
						</div>
					{/each}
				</div>
			</div>
		{/if}
	</Dialog.Content>
</Dialog.Root>

<!-- FileViewer for binary cell preview -->
<FileViewer
	bind:open={fileViewerOpen}
	url={fileViewerUrl}
	filename={fileViewerFilename}
	size={fileViewerSize}
	onclose={() => {
		fileViewerOpen = false;
	}}
/>
