<script lang="ts">
	import { Input } from '$lib/components/ui/input/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { Search, Download, Trash2, ChevronRight, ChevronDown, Info } from 'lucide-svelte';
	import { SvelteSet } from 'svelte/reactivity';
	import { formatBytes, formatDate } from '$lib/utils/format.js';
	import { toast } from 'svelte-sonner';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import {
		get_object_versions,
		delete_object_version,
		type ObjectVersion,
		type DeleteMarker,
	} from '$lib/remote/buckets.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		bucket,
		versioningStatus,
	}: {
		bucket: string;
		versioningStatus: string | null;
	} = $props();

	// --- Search & type filter ---
	let search = $state('');
	let typeFilter = $state<'all' | 'versions' | 'delete-markers'>('all');

	// --- Server-side pagination ---
	let keyMarker = $state<string | undefined>(undefined);
	let versionIdMarker = $state<string | undefined>(undefined);
	let tokenHistory = $state<{ keyMarker: string; versionIdMarker: string }[]>([]);

	let versionData = $derived(
		get_object_versions({
			bucket,
			prefix: search || undefined,
			key_marker: keyMarker,
			version_id_marker: versionIdMarker,
		})
	);

	let isTruncated = $derived(versionData.current?.is_truncated ?? false);
	let nextKeyMarker = $derived(versionData.current?.next_key_marker ?? null);
	let nextVersionIdMarker = $derived(versionData.current?.next_version_id_marker ?? null);

	function loadNextPage() {
		if (!nextKeyMarker) return;
		tokenHistory = [
			...tokenHistory,
			{ keyMarker: keyMarker ?? '', versionIdMarker: versionIdMarker ?? '' },
		];
		keyMarker = nextKeyMarker;
		versionIdMarker = nextVersionIdMarker ?? undefined;
	}

	function loadPrevPage() {
		if (tokenHistory.length === 0) return;
		const prev = tokenHistory[tokenHistory.length - 1];
		tokenHistory = tokenHistory.slice(0, -1);
		keyMarker = prev.keyMarker || undefined;
		versionIdMarker = prev.versionIdMarker || undefined;
	}

	$effect(() => {
		void search;
		keyMarker = undefined;
		versionIdMarker = undefined;
		tokenHistory = [];
	});

	// --- Unified row type ---
	interface VersionRow {
		key: string;
		versionId: string | null;
		isLatest: boolean;
		lastModified: string | null;
		size: number | null;
		etag: string | null;
		type: 'version' | 'delete-marker';
	}

	// --- Build rows (filter out VersionId: "null" entries) ---
	let allRows = $derived.by((): VersionRow[] => {
		const versions: VersionRow[] = ((versionData.current?.versions ?? []) as ObjectVersion[])
			.filter((v) => v.VersionId !== 'null')
			.map((v) => ({
				key: v.Key,
				versionId: v.VersionId,
				isLatest: v.IsLatest ?? false,
				lastModified: v.LastModified,
				size: v.Size,
				etag: v.ETag,
				type: 'version' as const,
			}));
		const markers: VersionRow[] = ((versionData.current?.delete_markers ?? []) as DeleteMarker[])
			.filter((d) => d.VersionId !== 'null')
			.map((d) => ({
				key: d.Key,
				versionId: d.VersionId,
				isLatest: d.IsLatest ?? false,
				lastModified: d.LastModified,
				size: null,
				etag: null,
				type: 'delete-marker' as const,
			}));
		return [...versions, ...markers];
	});

	let filteredRows = $derived(
		typeFilter === 'all'
			? allRows
			: typeFilter === 'versions'
				? allRows.filter((r) => r.type === 'version')
				: allRows.filter((r) => r.type === 'delete-marker')
	);

	// --- Group by key ---
	interface KeyGroup {
		key: string;
		rows: VersionRow[];
	}

	let groups = $derived.by((): KeyGroup[] => {
		const map = new Map<string, VersionRow[]>();
		for (const row of filteredRows) {
			const existing = map.get(row.key);
			if (existing) {
				existing.push(row);
			} else {
				map.set(row.key, [row]);
			}
		}
		return Array.from(map.entries()).map(([key, rows]) => ({ key, rows }));
	});

	// --- Multi-version groups (for expand/collapse logic) ---
	let multiVersionGroups = $derived(groups.filter((g) => g.rows.length > 1));

	// --- Expanded state (only tracks multi-version groups) ---
	let expanded = new SvelteSet<string>();

	function toggleGroup(key: string) {
		if (expanded.has(key)) {
			expanded.delete(key);
		} else {
			expanded.add(key);
		}
	}

	function expandAll() {
		for (const g of multiVersionGroups) {
			expanded.add(g.key);
		}
	}

	function collapseAll() {
		expanded.clear();
	}

	// Auto-expand multi-version groups when data loads
	$effect(() => {
		if (multiVersionGroups.length > 0 && multiVersionGroups.length <= 50) {
			expanded.clear();
			for (const g of multiVersionGroups) {
				expanded.add(g.key);
			}
		}
	});

	function getDisplayName(key: string): string {
		return key.split('/').filter(Boolean).pop() ?? key;
	}

	// --- Contextual header message ---
	let headerMessage = $derived.by(() => {
		if (versioningStatus === 'Enabled') {
			return 'Showing all object versions. New versions are created on each update.';
		}
		if (versioningStatus === 'Suspended') {
			return 'Versioning is suspended. Showing existing versions \u2014 no new versions are being created.';
		}
		return null;
	});

	// --- Download specific version ---
	function downloadVersion(row: VersionRow) {
		if (!row.versionId || row.type === 'delete-marker') return;
		const url = `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${encodeURIComponent(row.key)}?version_id=${encodeURIComponent(row.versionId)}`;
		const a = document.createElement('a');
		a.href = url;
		a.download = getDisplayName(row.key);
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
	}

	// --- Delete specific version ---
	let deleteDialogOpen = $state(false);
	let deleteTarget = $state<VersionRow | null>(null);
	let deleting = $state(false);

	function requestDelete(row: VersionRow) {
		deleteTarget = row;
		deleteDialogOpen = true;
	}

	async function handleConfirmDelete() {
		if (!deleteTarget?.versionId) return;
		deleting = true;
		try {
			await delete_object_version({
				bucket,
				key: deleteTarget.key,
				version_id: deleteTarget.versionId,
			}).updates(versionData);
			deleteDialogOpen = false;
			toast.success('Version deleted permanently');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete version'));
		} finally {
			deleting = false;
		}
	}

	let serverPage = $derived(tokenHistory.length + 1);
	let totalVersions = $derived(allRows.filter((r) => r.type === 'version').length);
	let totalMarkers = $derived(allRows.filter((r) => r.type === 'delete-marker').length);
</script>

{#snippet versionRow(row: VersionRow, showKey: boolean)}
	<div
		class="flex items-center gap-3 rounded-md px-4 py-2 text-sm {row.type === 'delete-marker'
			? 'bg-destructive/5'
			: 'hover:bg-muted/50'}"
	>
		<!-- Object key (only for single-version flat rows) -->
		{#if showKey}
			<span class="min-w-0 flex-1 truncate font-medium text-sm">{row.key}</span>
		{/if}

		<!-- Type badge -->
		<div class="w-28 shrink-0">
			{#if row.type === 'delete-marker'}
				<Badge variant="destructive">Delete Marker</Badge>
			{:else if row.isLatest}
				<Badge variant="success">Latest</Badge>
			{:else}
				<Badge variant="outline">Version</Badge>
			{/if}
		</div>

		<!-- Version ID -->
		<Tooltip.Root>
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<span {...props} class="w-32 shrink-0 truncate font-mono text-xs text-muted-foreground">
						{row.versionId
							? row.versionId.length > 12
								? row.versionId.slice(0, 12) + '...'
								: row.versionId
							: '-'}
					</span>
				{/snippet}
			</Tooltip.Trigger>
			<Tooltip.Content>
				<span class="font-mono text-xs">{row.versionId ?? 'No version ID'}</span>
			</Tooltip.Content>
		</Tooltip.Root>

		<!-- Size -->
		<span class="w-24 shrink-0 text-xs text-muted-foreground">
			{row.size != null ? formatBytes(row.size) : '-'}
		</span>

		<!-- Date -->
		<span class="{showKey ? 'w-40 shrink-0' : 'flex-1'} text-xs text-muted-foreground">
			{row.lastModified ? formatDate(row.lastModified) : '-'}
		</span>

		<!-- Actions -->
		{#if row.versionId}
			<div class="flex shrink-0 items-center gap-1">
				{#if row.type === 'version'}
					<Tooltip.Root>
						<Tooltip.Trigger>
							{#snippet child({ props })}
								<Button
									{...props}
									variant="ghost"
									size="icon"
									class="h-7 w-7"
									onclick={(e: MouseEvent) => {
										e.stopPropagation();
										downloadVersion(row);
									}}
								>
									<Download class="h-3.5 w-3.5" />
								</Button>
							{/snippet}
						</Tooltip.Trigger>
						<Tooltip.Content>Download this version</Tooltip.Content>
					</Tooltip.Root>
				{/if}
				<Tooltip.Root>
					<Tooltip.Trigger>
						{#snippet child({ props })}
							<Button
								{...props}
								variant="ghost"
								size="icon"
								class="h-7 w-7 text-muted-foreground hover:text-destructive"
								onclick={(e: MouseEvent) => {
									e.stopPropagation();
									requestDelete(row);
								}}
							>
								<Trash2 class="h-3.5 w-3.5" />
							</Button>
						{/snippet}
					</Tooltip.Trigger>
					<Tooltip.Content>
						{row.type === 'delete-marker'
							? 'Remove delete marker (restores object)'
							: 'Permanently delete this version'}
					</Tooltip.Content>
				</Tooltip.Root>
			</div>
		{/if}
	</div>
{/snippet}

{#await versionData}
	<TableSkeleton rows={5} columns={6} />
{:then}
	<div class="space-y-4">
		<!-- Contextual header -->
		{#if headerMessage}
			<div
				class="flex items-start gap-2 rounded-lg border border-border bg-muted/50 px-4 py-3 text-sm text-muted-foreground"
			>
				<Info class="mt-0.5 h-4 w-4 shrink-0" />
				<span>{headerMessage}</span>
			</div>
		{/if}

		<!-- Toolbar -->
		<div class="flex items-center gap-2">
			<div class="relative flex-1">
				<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
				<Input bind:value={search} placeholder="Filter by object key prefix..." class="pl-10" />
			</div>
			<select
				class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-auto min-w-[140px] items-center rounded-md border px-2 py-1 text-xs shadow-sm focus:outline-none focus:ring-1"
				bind:value={typeFilter}
			>
				<option value="all">All ({allRows.length})</option>
				<option value="versions">Versions ({totalVersions})</option>
				<option value="delete-markers">Delete Markers ({totalMarkers})</option>
			</select>
			{#if multiVersionGroups.length > 1}
				<Button variant="ghost" size="sm" class="h-8 text-xs" onclick={expandAll}>
					Expand all
				</Button>
				<Button variant="ghost" size="sm" class="h-8 text-xs" onclick={collapseAll}>
					Collapse all
				</Button>
			{/if}
			<span class="text-xs text-muted-foreground">
				{groups.length} object{groups.length !== 1 ? 's' : ''}, {filteredRows.length} version{filteredRows.length !==
				1
					? 's'
					: ''}{isTruncated ? ' (more available)' : ''}
			</span>
		</div>

		<!-- Grouped version list -->
		{#if versionData.current?.error}
			<div
				class="rounded-lg border border-destructive/20 bg-destructive/5 p-4 text-sm text-destructive"
			>
				{versionData.current.error}
			</div>
		{:else if groups.length === 0}
			<div class="rounded-lg border p-8 text-center text-sm text-muted-foreground">
				{search
					? `No versions matching "${search}"`
					: 'No object versions found yet. Versions will appear here as objects are updated or deleted.'}
			</div>
		{:else}
			<div class="space-y-1">
				{#each groups as group (group.key)}
					{#if group.rows.length === 1}
						<!-- Single-version object: flat row -->
						<div class="rounded-lg border bg-card">
							{@render versionRow(group.rows[0], true)}
						</div>
					{:else}
						<!-- Multi-version object: collapsible group -->
						{@const isExpanded = expanded.has(group.key)}
						{@const latestRow = group.rows.find((r) => r.isLatest)}
						{@const hasDeleteMarker = group.rows.some(
							(r) => r.type === 'delete-marker' && r.isLatest
						)}

						<!-- Group header -->
						<button
							class="flex w-full items-center gap-2 rounded-lg border bg-card px-4 py-2.5 text-left transition-colors hover:bg-accent/50"
							onclick={() => toggleGroup(group.key)}
						>
							{#if isExpanded}
								<ChevronDown class="h-4 w-4 shrink-0 text-muted-foreground" />
							{:else}
								<ChevronRight class="h-4 w-4 shrink-0 text-muted-foreground" />
							{/if}
							<span class="min-w-0 flex-1 truncate font-medium text-sm">{group.key}</span>
							{#if hasDeleteMarker}
								<Badge variant="destructive" class="shrink-0">Deleted</Badge>
							{:else if latestRow}
								<span class="shrink-0 text-xs text-muted-foreground">
									{latestRow.size != null ? formatBytes(latestRow.size) : ''}
								</span>
							{/if}
							<Badge variant="secondary" class="shrink-0">
								{group.rows.length} version{group.rows.length !== 1 ? 's' : ''}
							</Badge>
						</button>

						<!-- Expanded version rows -->
						{#if isExpanded}
							<div class="ml-6 space-y-0.5">
								{#each group.rows as row (row.versionId ?? row.type + row.lastModified)}
									{@render versionRow(row, false)}
								{/each}
							</div>
						{/if}
					{/if}
				{/each}
			</div>
		{/if}

		<!-- Server-side pagination -->
		{#if isTruncated || tokenHistory.length > 0}
			<div class="flex items-center justify-end gap-2">
				<span class="text-xs text-muted-foreground">Page {serverPage}</span>
				<Button
					variant="outline"
					size="sm"
					class="h-8"
					onclick={loadPrevPage}
					disabled={tokenHistory.length === 0}
				>
					Previous
				</Button>
				<Button
					variant="outline"
					size="sm"
					class="h-8"
					onclick={loadNextPage}
					disabled={!isTruncated}
				>
					Next
				</Button>
			</div>
		{/if}
	</div>
{/await}

<DeleteConfirmDialog
	bind:open={deleteDialogOpen}
	name={deleteTarget
		? `${getDisplayName(deleteTarget.key)} (${deleteTarget.type === 'delete-marker' ? 'delete marker' : 'version'} ${deleteTarget.versionId?.slice(0, 12) ?? ''})`
		: ''}
	itemType="version"
	loading={deleting}
	onconfirm={handleConfirmDelete}
/>
