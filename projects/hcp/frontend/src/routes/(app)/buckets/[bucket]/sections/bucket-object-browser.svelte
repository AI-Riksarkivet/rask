<script lang="ts">
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import {
		Trash2,
		Download,
		Folder,
		FileText,
		Image,
		FileArchive,
		FileCode,
		Loader2,
		Search,
		ListTree,
		FolderTree,
		Archive,
	} from 'lucide-svelte';
	import type { ColumnDef, SortingState, PaginationState } from '@tanstack/table-core';
	import FileViewer from '$lib/components/custom/file-viewer/FileViewer.svelte';
	import { formatBytes, formatDate } from '$lib/utils/format.js';
	import { toast } from 'svelte-sonner';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import BulkDeleteDialog from '$lib/components/custom/bulk-delete-dialog/bulk-delete-dialog.svelte';
	import {
		get_objects,
		start_s3_stats,
		delete_object,
		bulk_delete_objects,
		bulk_presign,
		start_zip_download,
	} from '$lib/remote/buckets.remote.js';
	import {
		DataTable,
		DataTableCheckbox,
		DataTableHeaderButton,
		createSvelteTable,
		getCoreRowModel,
		getSortedRowModel,
		getPaginationRowModel,
		renderSnippet,
		renderComponent,
	} from '$lib/components/ui/data-table/index.js';
	import DataTableActions from '../data-table/data-table-actions.svelte';
	import { onDestroy } from 'svelte';
	import BucketShareDialog from './bucket-share-dialog.svelte';
	import BucketCopyDialog from './bucket-copy-dialog.svelte';
	import BucketUploadDialog from './bucket-upload-dialog.svelte';
	import CreateFolderDialog from './create-folder-dialog.svelte';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		bucket,
		prefix,
		totalObjects = null,
		uploadOpen = $bindable(false),
		createFolderOpen = $bindable(false),
		onnavigate,
	}: {
		bucket: string;
		prefix: string;
		totalObjects?: number | null;
		uploadOpen: boolean;
		createFolderOpen: boolean;
		onnavigate: (prefix: string) => void;
	} = $props();

	// --- Flat mode (recursive listing for search across all objects) ---
	let flat = $state(false);

	let objectData = $derived(get_objects({ bucket, prefix, flat }));
	let isTruncated = $derived(objectData.current?.isTruncated ?? false);

	// On-demand count — user triggers via button, polls for result
	let prefixCount = $state<{ files: number; folders: number } | null>(null);
	let counting = $state(false);
	let countPollInterval: ReturnType<typeof setInterval> | undefined;

	onDestroy(() => clearInterval(countPollInterval));

	async function countObjects() {
		counting = true;
		prefixCount = null;
		clearInterval(countPollInterval);
		try {
			const result = await start_s3_stats({
				bucket,
				prefix: prefix || undefined,
				delimiter: flat ? undefined : '/',
			});
			pollStatsTask(result.task_id);
		} catch {
			counting = false;
		}
	}

	function pollStatsTask(taskId: string) {
		countPollInterval = setInterval(async () => {
			try {
				const res = await fetch(
					`/api/v1/buckets/${encodeURIComponent(bucket)}/objects/s3_stats/${encodeURIComponent(taskId)}`
				);
				if (!res.ok) {
					clearInterval(countPollInterval);
					counting = false;
					return;
				}
				const data = await res.json();
				prefixCount = { files: data.files, folders: data.folders };
				if (data.status === 'ready' || data.status === 'failed') {
					clearInterval(countPollInterval);
					counting = false;
				}
			} catch {
				clearInterval(countPollInterval);
				counting = false;
			}
		}, 2000);
	}

	// Reset count when navigating
	$effect(() => {
		void prefix;
		void flat;
		prefixCount = null;
		counting = false;
		clearInterval(countPollInterval);
	});

	// When navigating into a selected folder, select all items in the new view
	let selectAllOnLoad = $state(false);

	// Reset row selection when prefix or flat mode changes
	$effect(() => {
		void prefix;
		void flat;
		if (!selectAllOnLoad) {
			rowSelection = {};
		}
	});

	// Select all rows after data loads (when navigating into a selected folder)
	$effect(() => {
		if (selectAllOnLoad && filteredObjects.length > 0) {
			const allSelected: Record<string, boolean> = {};
			for (let i = 0; i < filteredObjects.length; i++) {
				allSelected[String(i)] = true;
			}
			rowSelection = allSelected;
			selectAllOnLoad = false;
		}
	});

	// --- S3 Object type ---
	interface S3Object {
		key: string;
		size: number;
		last_modified: string;
		etag: string;
		storage_class: string;
		owner: { display_name?: string; id?: string; DisplayName?: string; ID?: string } | null;
	}

	function getOwnerName(obj: S3Object): string {
		return obj.owner?.display_name ?? obj.owner?.DisplayName ?? '';
	}

	// --- Object list ---
	let rawObjects = $derived(
		((objectData.current?.objects ?? []) as S3Object[]).filter(
			(o) => o.key !== prefix && !(flat && o.key.endsWith('/'))
		)
	);
	let commonPrefixes = $derived(
		flat ? [] : ((objectData.current?.commonPrefixes ?? []) as string[])
	);
	let folderEntries = $derived(
		commonPrefixes.map(
			(p): S3Object => ({
				key: p,
				size: 0,
				last_modified: '',
				etag: '',
				storage_class: '',
				owner: null,
			})
		)
	);
	let objects = $derived([...folderEntries, ...rawObjects]);

	function downloadUrl(key: string): string {
		return `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${encodeURIComponent(key)}`;
	}
	function getDisplayName(key: string): string {
		if (flat && prefix && key.startsWith(prefix)) {
			return key.slice(prefix.length) || key;
		}
		return key.split('/').filter(Boolean).pop() ?? key;
	}
	function isObjFolder(obj: S3Object): boolean {
		return obj.size === 0 && obj.key.endsWith('/');
	}

	function getFileIcon(name: string) {
		const ext = name.split('.').pop()?.toLowerCase() ?? '';
		if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'].includes(ext)) return Image;
		if (['zip', 'tar', 'gz', 'rar', '7z'].includes(ext)) return FileArchive;
		if (['js', 'ts', 'py', 'json', 'html', 'css', 'xml', 'yaml', 'yml', 'toml'].includes(ext))
			return FileCode;
		return FileText;
	}

	// --- Filters (client-side within the loaded page) ---
	let search = $state('');
	let ownerFilter = $state('');
	let sizeFilter = $state('');
	let typeFilter = $state('');
	let storageClassFilter = $state('');
	let dateFilter = $state('');

	let uniqueOwners = $derived([...new Set(rawObjects.map(getOwnerName).filter(Boolean))]);
	let uniqueStorageClasses = $derived(
		[...new Set(rawObjects.map((o) => o.storage_class).filter(Boolean))].sort()
	);

	function getFileType(key: string): string {
		const ext = key.split('.').pop()?.toLowerCase() ?? '';
		if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'ico'].includes(ext)) return 'Images';
		if (
			[
				'pdf',
				'doc',
				'docx',
				'xls',
				'xlsx',
				'ppt',
				'pptx',
				'odt',
				'ods',
				'txt',
				'rtf',
				'csv',
			].includes(ext)
		)
			return 'Documents';
		if (['zip', 'tar', 'gz', 'rar', '7z', 'bz2', 'xz'].includes(ext)) return 'Archives';
		if (
			[
				'js',
				'ts',
				'py',
				'json',
				'html',
				'css',
				'xml',
				'yaml',
				'yml',
				'toml',
				'sh',
				'java',
				'c',
				'cpp',
				'go',
				'rs',
			].includes(ext)
		)
			return 'Code';
		if (['mp4', 'mov', 'avi', 'mkv', 'mp3', 'wav', 'flac', 'ogg'].includes(ext)) return 'Media';
		if (['log', 'bak', 'tmp'].includes(ext)) return 'Logs/Temp';
		return 'Other';
	}

	let uniqueFileTypes = $derived.by(() => {
		const types = new Set<string>();
		for (const o of rawObjects) types.add(getFileType(o.key));
		return [...types].sort();
	});

	function matchesDateFilter(dateStr: string, filter: string): boolean {
		if (!dateStr || !filter) return true;
		const d = new Date(dateStr);
		const now = new Date();
		if (filter === '24h') return now.getTime() - d.getTime() < 86400000;
		if (filter === '7d') return now.getTime() - d.getTime() < 604800000;
		if (filter === '30d') return now.getTime() - d.getTime() < 2592000000;
		return true;
	}

	let filteredObjects = $derived(
		objects.filter((obj) => {
			if (search) {
				const q = search.toLowerCase();
				if (
					!getDisplayName(obj.key).toLowerCase().includes(q) &&
					!obj.key.toLowerCase().includes(q)
				)
					return false;
			}
			if (ownerFilter && getOwnerName(obj) !== ownerFilter && !isObjFolder(obj)) return false;
			if (sizeFilter && !isObjFolder(obj)) {
				const s = obj.size;
				if (sizeFilter === '<1KB' && s >= 1024) return false;
				if (sizeFilter === '1KB-1MB' && (s < 1024 || s >= 1048576)) return false;
				if (sizeFilter === '1MB-100MB' && (s < 1048576 || s >= 104857600)) return false;
				if (sizeFilter === '>100MB' && s < 104857600) return false;
			}
			if (typeFilter && !isObjFolder(obj) && getFileType(obj.key) !== typeFilter) return false;
			if (storageClassFilter && !isObjFolder(obj) && obj.storage_class !== storageClassFilter)
				return false;
			if (dateFilter && !isObjFolder(obj) && !matchesDateFilter(obj.last_modified, dateFilter))
				return false;
			return true;
		})
	);

	let hasActiveFilters = $derived(
		!!ownerFilter || !!sizeFilter || !!typeFilter || !!storageClassFilter || !!dateFilter
	);
	function clearFilters() {
		ownerFilter = '';
		sizeFilter = '';
		typeFilter = '';
		storageClassFilter = '';
		dateFilter = '';
	}

	// --- TanStack Table with sorting + pagination (standard pattern) ---
	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 50 });

	let selectableObjects = $derived(filteredObjects.filter((obj) => !isObjFolder(obj)));

	// Row selection via TanStack
	let rowSelection = $state<Record<string, boolean>>({});

	let selectedKeys = $derived(
		Object.entries(rowSelection)
			.filter(([, v]) => v)
			.map(([idx]) => {
				const row = objTable.getCoreRowModel().rows[Number(idx)];
				return row?.original.key;
			})
			.filter((k): k is string => !!k)
	);

	let objColumns = $derived.by((): ColumnDef<S3Object>[] => [
		{
			id: 'select',
			header: ({ table }) =>
				renderComponent(DataTableCheckbox, {
					checked: table.getIsAllPageRowsSelected(),
					indeterminate: table.getIsSomePageRowsSelected() && !table.getIsAllPageRowsSelected(),
					onCheckedChange: (value: boolean) => table.toggleAllPageRowsSelected(!!value),
					'aria-label': 'Select all rows',
				}),
			cell: ({ row }) =>
				renderComponent(DataTableCheckbox, {
					checked: row.getIsSelected(),
					onCheckedChange: (value: boolean) => row.toggleSelected(!!value),
					'aria-label': `Select ${getDisplayName(row.original.key)}`,
				}),
			enableSorting: false,
			enableHiding: false,
			meta: { headerClass: 'w-10 px-4 py-3', cellClass: 'px-4 py-3' },
		},
		{
			id: 'name',
			accessorFn: (row) => getDisplayName(row.key),
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Name',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nameCellSnippet, row.original),
		},
		{
			id: 'owner',
			accessorFn: (row) => getOwnerName(row),
			header: 'Owner',
			cell: ({ row }) =>
				(isObjFolder(row.original) ? '-' : getOwnerName(row.original) || '-') as string,
			meta: { headerClass: 'w-32 px-4 py-3', cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'size',
			accessorFn: (row) => row.size,
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Size',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) =>
				(isObjFolder(row.original) ? '-' : formatBytes(row.original.size)) as string,
			meta: { headerClass: 'w-32 px-4 py-3', cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'lastModified',
			accessorFn: (row) => row.last_modified,
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Modified',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) =>
				(row.original.last_modified ? formatDate(row.original.last_modified) : '-') as string,
			meta: { headerClass: 'w-48 px-4 py-3', cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCellSnippet, row.original),
			enableSorting: false,
			meta: { headerClass: 'w-16 px-4 py-3', cellClass: 'px-4 py-3' },
		},
	]);

	let objTable = $derived(
		createSvelteTable({
			get data() {
				return filteredObjects;
			},
			get columns() {
				return objColumns;
			},
			state: {
				get sorting() {
					return sorting;
				},
				get pagination() {
					return pagination;
				},
				get rowSelection() {
					return rowSelection;
				},
			},
			onSortingChange: (updater) => {
				sorting = typeof updater === 'function' ? updater(sorting) : updater;
			},
			onPaginationChange: (updater) => {
				pagination = typeof updater === 'function' ? updater(pagination) : updater;
			},
			onRowSelectionChange: (updater) => {
				rowSelection = typeof updater === 'function' ? updater(rowSelection) : updater;
			},
			getCoreRowModel: getCoreRowModel(),
			getSortedRowModel: getSortedRowModel(),
			getPaginationRowModel: getPaginationRowModel(),
			enableRowSelection: true,
		})
	);

	// --- Delete ---
	let deleteDialogOpen = $state(false);
	let deleteTarget = $state('');
	let deleting = $state(false);
	let bulkDeleteOpen = $state(false);

	function requestDelete(key: string) {
		deleteTarget = key;
		deleteDialogOpen = true;
	}

	async function handleConfirmDelete() {
		deleting = true;
		try {
			await delete_object({ bucket, key: deleteTarget }).updates(objectData);
			deleteDialogOpen = false;
			toast.success('Object deleted');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete object'));
		} finally {
			deleting = false;
		}
	}

	async function handleConfirmBulkDelete() {
		const keys = selectedKeys;
		if (keys.length === 0) return;
		deleting = true;
		try {
			await bulk_delete_objects({ bucket, keys }).updates(objectData);
			rowSelection = {};
			bulkDeleteOpen = false;
			toast.success(`Deleted ${keys.length} object${keys.length !== 1 ? 's' : ''}`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete objects'));
		} finally {
			deleting = false;
		}
	}

	// --- Download a single file via proxy ---
	function handleDownload(key: string) {
		const a = document.createElement('a');
		a.href = downloadUrl(key);
		a.download = key.split('/').pop() ?? key;
		a.style.display = 'none';
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
	}

	// --- Dialogs ---
	let shareTarget = $state<string | null>(null);
	let copyTarget = $state<string | null>(null);
	let downloading = $state(false);
	let usePresigned = $state(false);

	async function bulkDownload() {
		const keys = selectedKeys;
		if (keys.length === 0) return;

		const fileKeys = keys.filter((k) => !k.endsWith('/'));
		const folderKeys = keys.filter((k) => k.endsWith('/'));

		// Single file — direct download
		if (fileKeys.length === 1 && folderKeys.length === 0) {
			if (usePresigned) {
				try {
					const result = await bulk_presign({ bucket, keys: fileKeys, expires_in: 3600 });
					if (result.urls.length > 0) {
						const a = document.createElement('a');
						a.href = result.urls[0].url;
						a.download = fileKeys[0].split('/').pop() ?? fileKeys[0];
						a.style.display = 'none';
						document.body.appendChild(a);
						a.click();
						document.body.removeChild(a);
					}
				} catch (err) {
					toast.error(getErrorMessage(err, 'Failed to download file'));
				}
			} else {
				handleDownload(fileKeys[0]);
			}
			return;
		}

		// Multiple files and/or folders — ZIP download
		zipDownloading = true;
		zipProgress = { total: 0, completed: 0, failed: 0 };
		try {
			const body: { bucket: string; prefix?: string; keys?: string[] } = { bucket };
			if (folderKeys.length === 1 && fileKeys.length === 0) {
				body.prefix = folderKeys[0];
			} else if (folderKeys.length > 0) {
				body.prefix = folderKeys[0];
			} else {
				body.keys = fileKeys;
			}
			const result = await start_zip_download(body);
			zipTaskId = result.task_id;
			zipProgress.total = result.total;
			toast.info(`Preparing ZIP... ${fileKeys.length + folderKeys.length} items`);
			pollZipStatus();
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to start ZIP download'));
			zipDownloading = false;
		}
	}

	// --- ZIP Download All ---
	let zipDownloading = $state(false);
	let zipProgress = $state({ total: 0, completed: 0, failed: 0 });
	let zipTaskId = $state<string | null>(null);
	let zipPollInterval: ReturnType<typeof setInterval> | undefined;

	onDestroy(() => clearInterval(zipPollInterval));

	async function startZipForPrefix(zipPrefix: string) {
		zipDownloading = true;
		zipProgress = { total: 0, completed: 0, failed: 0 };
		try {
			const result = await start_zip_download({ bucket, prefix: zipPrefix });
			zipTaskId = result.task_id;
			zipProgress.total = result.total;
			toast.info(`Preparing ZIP... 0/${result.total} files`);
			pollZipStatus();
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to start ZIP download'));
			zipDownloading = false;
		}
	}

	async function startZipDownload() {
		await startZipForPrefix(prefix || '');
	}

	function pollZipStatus() {
		if (!zipTaskId) return;
		const tid = zipTaskId;
		clearInterval(zipPollInterval);
		zipPollInterval = setInterval(async () => {
			try {
				const res = await fetch(
					`/api/v1/buckets/${encodeURIComponent(bucket)}/objects/download/${encodeURIComponent(tid)}`
				);
				const contentType = res.headers.get('content-type') ?? '';

				if (
					contentType.includes('application/zip') ||
					contentType.includes('application/octet-stream')
				) {
					clearInterval(zipPollInterval);
					const blob = await res.blob();
					const url = URL.createObjectURL(blob);
					const a = document.createElement('a');
					a.href = url;
					a.download = `${bucket}-objects.zip`;
					document.body.appendChild(a);
					a.click();
					document.body.removeChild(a);
					URL.revokeObjectURL(url);

					if (zipProgress.failed > 0) {
						toast.warning(
							`ZIP ready — ${zipProgress.failed} file${zipProgress.failed !== 1 ? 's' : ''} could not be included`
						);
					} else {
						toast.success('ZIP download complete');
					}
					zipDownloading = false;
					zipTaskId = null;
					return;
				}

				if (res.ok && contentType.includes('application/json')) {
					const data = await res.json();
					zipProgress = {
						total: data.total ?? zipProgress.total,
						completed: data.completed ?? 0,
						failed: data.failed ?? 0,
					};

					if (data.status === 'failed') {
						clearInterval(zipPollInterval);
						toast.error('ZIP download failed');
						zipDownloading = false;
						zipTaskId = null;
						return;
					}
				}

				if (!res.ok) {
					// 404 after successful download is expected (task cleaned up)
					if (res.status === 404 && !zipDownloading) return;
					clearInterval(zipPollInterval);
					toast.error('ZIP download failed');
					zipDownloading = false;
					zipTaskId = null;
				}
			} catch {
				clearInterval(zipPollInterval);
				toast.error('Failed to check ZIP status');
				zipDownloading = false;
				zipTaskId = null;
			}
		}, 2000);
	}

	// --- File Viewer ---
	let viewerOpen = $state(false);
	let viewerIndex = $state(-1);

	let viewerObject = $derived(viewerIndex >= 0 ? selectableObjects[viewerIndex] : undefined);
	let viewerUrl = $derived(viewerObject ? downloadUrl(viewerObject.key) : '');
	let viewerFilename = $derived(viewerObject ? getDisplayName(viewerObject.key) : '');
	let viewerSize = $derived(viewerObject?.size ?? 0);
	let viewerHasPrev = $derived(viewerIndex > 0);
	let viewerHasNext = $derived(viewerIndex < selectableObjects.length - 1);

	function openViewer(obj: { key: string; size: number }) {
		viewerIndex = selectableObjects.findIndex((o) => o.key === obj.key);
		viewerOpen = true;
	}

	function handleRowClick(obj: S3Object) {
		if (isObjFolder(obj)) {
			selectAllOnLoad = selectedKeys.includes(obj.key);
			onnavigate(obj.key);
		} else {
			openViewer(obj);
		}
	}
</script>

{#snippet nameCellSnippet(obj: S3Object)}
	<span class="flex items-center gap-2">
		{#if isObjFolder(obj)}
			<Folder class="h-4 w-4 text-amber-500" />
		{:else}
			{@const Icon = getFileIcon(obj.key)}
			<Icon class="h-4 w-4 text-muted-foreground" />
		{/if}
		<span class="font-medium">{getDisplayName(obj.key)}</span>
	</span>
{/snippet}

{#snippet actionsCellSnippet(obj: S3Object)}
	<span
		onclick={(e: MouseEvent) => e.stopPropagation()}
		onkeydown={(e: KeyboardEvent) => e.stopPropagation()}
		role="presentation"
	>
		{#if !isObjFolder(obj)}
			<DataTableActions
				objectKey={obj.key}
				ondownload={() => handleDownload(obj.key)}
				ondelete={() => requestDelete(obj.key)}
				onshare={() => (shareTarget = obj.key)}
				onview={() => openViewer(obj)}
				oncopy={() => (copyTarget = obj.key)}
			/>
		{/if}
	</span>
{/snippet}

<!-- Object Table -->
{#await objectData}
	<TableSkeleton rows={5} columns={7} />
{:then}
	<div class="space-y-2">
		<div class="flex items-center gap-2">
			<div class="relative flex-1">
				<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
				<Input
					bind:value={search}
					placeholder={flat
						? 'Search all objects recursively...'
						: 'Search objects in current folder...'}
					class="pl-10"
				/>
			</div>
			<Tooltip.Root>
				<Tooltip.Trigger>
					{#snippet child({ props })}
						<Button
							{...props}
							variant={flat ? 'default' : 'outline'}
							size="icon"
							class="h-9 w-9 shrink-0"
							onclick={() => (flat = !flat)}
						>
							{#if flat}
								<ListTree class="h-4 w-4" />
							{:else}
								<FolderTree class="h-4 w-4" />
							{/if}
						</Button>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content side="bottom" class="max-w-xs">
					{#if flat}
						<strong>Flat view</strong> — showing all objects recursively. Search works across all files.
						Click to switch back to folder view.
					{:else}
						<strong>Folder view</strong> — showing current folder only. Click to flatten and search across
						all objects recursively.
					{/if}
				</Tooltip.Content>
			</Tooltip.Root>
			<Tooltip.Root>
				<Tooltip.Trigger>
					{#snippet child({ props })}
						<Button
							{...props}
							variant="outline"
							size="icon"
							class="h-9 w-9 shrink-0"
							onclick={startZipDownload}
							disabled={zipDownloading}
						>
							{#if zipDownloading}
								<Loader2 class="h-4 w-4 animate-spin" />
							{:else}
								<Archive class="h-4 w-4" />
							{/if}
						</Button>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content side="bottom" class="max-w-xs">
					{#if zipDownloading}
						<strong>Preparing ZIP...</strong> {zipProgress.completed}/{zipProgress.total} files
					{:else}
						<strong>Download All as ZIP</strong> — download all objects under the current prefix as a
						single ZIP file
					{/if}
				</Tooltip.Content>
			</Tooltip.Root>
		</div>
		<div class="flex flex-wrap items-center gap-2">
			{#if uniqueOwners.length > 0}
				<select
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-auto min-w-[120px] items-center rounded-md border px-2 py-1 text-xs shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={ownerFilter}
				>
					<option value="">All owners</option>
					{#each uniqueOwners as o (o)}<option value={o}>{o}</option>{/each}
				</select>
			{/if}
			<select
				class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-auto min-w-[120px] items-center rounded-md border px-2 py-1 text-xs shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
				bind:value={sizeFilter}
			>
				<option value="">All sizes</option>
				<option value="<1KB">&lt; 1 KB</option>
				<option value="1KB-1MB">1 KB - 1 MB</option>
				<option value="1MB-100MB">1 MB - 100 MB</option>
				<option value=">100MB">&gt; 100 MB</option>
			</select>
			{#if uniqueFileTypes.length > 1}
				<select
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-auto min-w-[120px] items-center rounded-md border px-2 py-1 text-xs shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={typeFilter}
				>
					<option value="">All types</option>
					{#each uniqueFileTypes as t (t)}<option value={t}>{t}</option>{/each}
				</select>
			{/if}
			{#if uniqueStorageClasses.length > 1}
				<select
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-auto min-w-[120px] items-center rounded-md border px-2 py-1 text-xs shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={storageClassFilter}
				>
					<option value="">All storage classes</option>
					{#each uniqueStorageClasses as sc (sc)}<option value={sc}>{sc}</option>{/each}
				</select>
			{/if}
			<select
				class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-auto min-w-[120px] items-center rounded-md border px-2 py-1 text-xs shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
				bind:value={dateFilter}
			>
				<option value="">All dates</option>
				<option value="24h">Last 24 hours</option>
				<option value="7d">Last 7 days</option>
				<option value="30d">Last 30 days</option>
			</select>
			{#if hasActiveFilters}
				<Button variant="ghost" size="sm" class="h-7 px-2 text-xs" onclick={clearFilters}>
					Clear filters
				</Button>
			{/if}
			<span class="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
				{#if prefixCount && !counting}
					<Tooltip.Root>
						<Tooltip.Trigger>
							{#snippet child({ props })}
								<span {...props}>
									{prefixCount.files.toLocaleString()} file{prefixCount.files !== 1 ? 's' : ''}{prefixCount.folders > 0 ? `, ${prefixCount.folders.toLocaleString()} folder${prefixCount.folders !== 1 ? 's' : ''}` : ''}
								</span>
							{/snippet}
						</Tooltip.Trigger>
						<Tooltip.Content side="bottom" class="max-w-xs">
							Exact count from S3. {flat ? 'Flat view counts all objects recursively.' : 'Folder view counts files and folders at this level.'}
						</Tooltip.Content>
					</Tooltip.Root>
				{:else if counting && prefixCount}
					<Loader2 class="h-3 w-3 animate-spin" />
					{prefixCount.files.toLocaleString()} file{prefixCount.files !== 1 ? 's' : ''} counted...
				{:else if counting}
					<Loader2 class="h-3 w-3 animate-spin" />
					Counting...
				{:else}
					{#if isTruncated}
						<Tooltip.Root>
							<Tooltip.Trigger>
								{#snippet child({ props })}
									<span {...props}>
										{rawObjects.length.toLocaleString()}+ file{rawObjects.length !== 1 ? 's' : ''}{commonPrefixes.length > 0 ? `, ${commonPrefixes.length}+ folder${commonPrefixes.length !== 1 ? 's' : ''}` : ''}
									</span>
								{/snippet}
							</Tooltip.Trigger>
							<Tooltip.Content side="bottom" class="max-w-xs">
								S3 returns up to 1,000 items per request. Click "Count all" to get the exact total — this runs in the background and may take a while for large {flat ? 'buckets' : 'folders'}.
							</Tooltip.Content>
						</Tooltip.Root>
						<Button variant="ghost" size="sm" class="h-6 px-2 text-xs" onclick={countObjects}>
							Count all
						</Button>
					{:else}
						<Tooltip.Root>
							<Tooltip.Trigger>
								{#snippet child({ props })}
									<span {...props}>
										{rawObjects.length.toLocaleString()} file{rawObjects.length !== 1 ? 's' : ''}{commonPrefixes.length > 0 ? `, ${commonPrefixes.length} folder${commonPrefixes.length !== 1 ? 's' : ''}` : ''}
									</span>
								{/snippet}
							</Tooltip.Trigger>
							<Tooltip.Content side="bottom" class="max-w-xs">
								Exact count. {flat ? 'All objects in this bucket.' : 'Files and folders at this level.'}
							</Tooltip.Content>
						</Tooltip.Root>
					{/if}
				{/if}
			</span>
		</div>
	</div>

	{#if selectedKeys.length > 0}
		<div class="flex items-center gap-3 rounded-lg border bg-muted/50 px-4 py-2">
			<span class="text-sm font-medium">{selectedKeys.length} selected</span>
			<Button size="sm" onclick={bulkDownload} disabled={downloading}
				>{#if downloading}<Loader2 class="h-3.5 w-3.5 animate-spin" />{:else}<Download
						class="h-3.5 w-3.5"
					/>{/if}Download Selected</Button
			>
			<Tooltip.Root>
				<Tooltip.Trigger>
					{#snippet child({ props })}
						<label class="flex items-center gap-1.5 text-xs text-muted-foreground" {...props}>
							<input type="checkbox" bind:checked={usePresigned} class="h-3.5 w-3.5 rounded border-muted-foreground" />
							Presigned
						</label>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content side="bottom" class="max-w-xs">
					<strong>Presigned URLs</strong> download directly from HCP S3 — faster, but requires network access to the internal HCP endpoint. Disable if downloads are blocked by VPN/firewall.
				</Tooltip.Content>
			</Tooltip.Root>
			<Button variant="destructive" size="sm" onclick={() => (bulkDeleteOpen = true)}
				><Trash2 class="h-3.5 w-3.5" />Delete Selected</Button
			>
			<Button variant="ghost" size="sm" onclick={() => (rowSelection = {})}>Deselect All</Button>
		</div>
	{/if}

	<DataTable
		table={objTable}
		onrowclick={handleRowClick}
		noResultsMessage={objectData.current?.error
			? objectData.current.error
			: objects.length === 0
				? 'No objects in this bucket'
				: `No results matching "${search}"`}
	>
		{#snippet footer()}
			{#if selectedKeys.length > 0}
				{selectedKeys.length} of {filteredObjects.length} row(s) selected.
			{/if}
		{/snippet}
	</DataTable>

	{#if isTruncated}
		<p class="py-1 text-center text-xs text-amber-600">
			{#if prefixCount}
				Showing {filteredObjects.length.toLocaleString()} of {(prefixCount.files + prefixCount.folders).toLocaleString()} total. Use search to narrow results.
			{:else}
				Showing first {filteredObjects.length.toLocaleString()} objects. Use search to narrow results.
			{/if}
		</p>
	{/if}
{/await}

<!-- Dialogs -->
<BucketUploadDialog
	bind:open={uploadOpen}
	{bucket}
	{prefix}
	onuploaded={() => objectData.refresh()}
/>

<BucketShareDialog {bucket} bind:target={shareTarget} />

<BucketCopyDialog {bucket} bind:target={copyTarget} oncopied={() => objectData.refresh()} />

{#if viewerOpen}<FileViewer
		bind:open={viewerOpen}
		url={viewerUrl}
		filename={viewerFilename}
		size={viewerSize}
		objectKey={viewerObject?.key}
		lastModified={viewerObject?.last_modified}
		etag={viewerObject?.etag}
		storageClass={viewerObject?.storage_class}
		hasPrev={viewerHasPrev}
		hasNext={viewerHasNext}
		onprev={() => {
			if (viewerHasPrev) viewerIndex--;
		}}
		onnext={() => {
			if (viewerHasNext) viewerIndex++;
		}}
		currentIndex={viewerIndex}
		totalCount={selectableObjects.length}
		onclose={() => (viewerOpen = false)}
	/>{/if}

<DeleteConfirmDialog
	bind:open={deleteDialogOpen}
	name={deleteTarget ? getDisplayName(deleteTarget) : ''}
	itemType="object"
	loading={deleting}
	onconfirm={handleConfirmDelete}
/>

<BulkDeleteDialog
	bind:open={bulkDeleteOpen}
	count={selectedKeys.length}
	itemType="object"
	loading={deleting}
	onconfirm={handleConfirmBulkDelete}
/>

<CreateFolderDialog
	bind:open={createFolderOpen}
	{bucket}
	{prefix}
	oncreated={() => objectData.refresh()}
/>
