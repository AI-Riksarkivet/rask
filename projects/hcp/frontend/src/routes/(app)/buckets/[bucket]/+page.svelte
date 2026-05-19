<script lang="ts">
	import { page } from '$app/state';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import {
		Upload,
		Folder,
		FolderOpen,
		FolderPlus,
		Shield,
		History,
		Globe,
		UploadCloud,
	} from 'lucide-svelte';
	import {
		formatBytes,
		parseQuotaBytes,
		getStorageUsed,
		calcQuotaPercent,
	} from '$lib/utils/format.js';
	import type { ChargebackEntry } from '$lib/utils/format.js';
	import { goto } from '$app/navigation';
	import BackButton from '$lib/components/custom/back-button/back-button.svelte';
	import StorageProgressBar from '$lib/components/custom/storage-progress-bar/storage-progress-bar.svelte';
	import { get_tenant_chargeback } from '$lib/remote/tenant-info.remote.js';
	import {
		get_namespaces,
		get_ns_statistics,
		type Namespace,
		type NsStatistics,
	} from '$lib/remote/namespaces.remote.js';
	import { get_bucket_versioning, type BucketVersioning } from '$lib/remote/buckets.remote.js';
	import BucketVersioningControl from './sections/bucket-versioning.svelte';
	import BucketAcl from './sections/bucket-acl.svelte';
	import BucketObjectBrowser from './sections/bucket-object-browser.svelte';
	import BucketVersions from './sections/bucket-versions.svelte';
	import BucketCors from './sections/bucket-cors.svelte';
	import BucketUploads from './sections/bucket-uploads.svelte';

	// --- Page params ---
	let tenant = $derived(page.data.tenant as string | undefined);
	let bucket = $derived(page.params.bucket ?? '');
	let prefix = $derived(page.url.searchParams.get('prefix') ?? '');

	// --- Tenant / storage data ---
	let chargebackData = $derived(tenant ? get_tenant_chargeback({ tenant }) : undefined);
	let nsData = $derived(tenant ? get_namespaces({ tenant }) : undefined);

	let bucketStorageUsed = $derived(
		getStorageUsed((chargebackData?.current?.chargebackData ?? []) as ChargebackEntry[], bucket)
	);
	let bucketQuotaStr = $derived.by(() => {
		const nsList = (nsData?.current ?? []) as Namespace[];
		const ns = nsList.find((n) => n.name === bucket);
		return ns?.hardQuota ?? null;
	});
	let bucketQuotaBytes = $derived(bucketQuotaStr ? parseQuotaBytes(bucketQuotaStr) : null);
	let bucketQuotaPercent = $derived(calcQuotaPercent(bucketStorageUsed, bucketQuotaStr));

	// --- Namespace statistics (total object count) — may be null if user lacks MONITOR permission ---
	let statsData = $derived(tenant ? get_ns_statistics({ tenant, name: bucket }) : undefined);
	let totalObjects = $derived((statsData?.current as NsStatistics | null)?.objectCount ?? null);

	// --- Versioning status (used to conditionally show Versions tab) ---
	let versioningData = $derived(get_bucket_versioning({ bucket }));
	let versioning = $derived((versioningData?.current ?? {}) as BucketVersioning);
	let showVersionsTab = $derived(
		versioning.status === 'Enabled' || versioning.status === 'Suspended'
	);

	// --- Navigation ---
	function navigatePrefix(p: string) {
		goto(`/buckets/${bucket}?prefix=${encodeURIComponent(p)}`);
	}

	// --- Breadcrumbs ---
	let breadcrumbs = $derived.by(() => {
		const crumbs: { label: string; prefix: string }[] = [{ label: bucket, prefix: '' }];
		if (!prefix) return crumbs;
		const parts = prefix.replace(/\/$/, '').split('/');
		let accumulated = '';
		for (const part of parts) {
			accumulated += part + '/';
			crumbs.push({ label: part, prefix: accumulated });
		}
		return crumbs;
	});

	// --- Active tab ---
	let activeTab = $state('objects');

	// --- Upload dialog (controlled from header, rendered inside object browser) ---
	let uploadOpen = $state(false);
	let createFolderOpen = $state(false);
</script>

<svelte:head><title>{bucket} - HCP Admin Console</title></svelte:head>

<div class="space-y-6">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-4">
			<BackButton href="/buckets" label="Back to buckets" />
			<div>
				<h2 class="text-2xl font-bold">{bucket}</h2>
			</div>
			{#if totalObjects !== null}
				<span class="text-xs text-muted-foreground">
					{totalObjects.toLocaleString()} object{totalObjects !== 1 ? 's' : ''}
				</span>
			{/if}
			{#if tenant && (bucketStorageUsed > 0 || bucketQuotaBytes)}
				<Tooltip.Root>
					<Tooltip.Trigger>
						{#snippet child({ props })}
							<span class="flex items-center gap-1.5 text-xs text-muted-foreground" {...props}>
								<span
									>{formatBytes(bucketStorageUsed)}{bucketQuotaStr
										? ` / ${bucketQuotaStr}`
										: ''}</span
								>
								{#if bucketQuotaPercent !== null}
									<StorageProgressBar percent={bucketQuotaPercent} class="w-16" />
								{/if}
							</span>
						{/snippet}
					</Tooltip.Trigger>
					<Tooltip.Content>Bucket storage usage</Tooltip.Content>
				</Tooltip.Root>
			{/if}
			<BucketVersioningControl {bucket} />
		</div>
		{#if activeTab === 'objects'}
			<div class="flex items-center gap-2">
				<Button variant="outline" onclick={() => (createFolderOpen = true)}
					><FolderPlus class="h-4 w-4" />New Folder</Button
				>
				<Button onclick={() => (uploadOpen = true)}><Upload class="h-4 w-4" />Upload Files</Button>
			</div>
		{/if}
	</div>

	<!-- Tabs -->
	<Tabs.Root bind:value={activeTab}>
		<Tabs.List>
			<Tabs.Trigger value="objects">
				<FolderOpen class="mr-1.5 h-4 w-4" />
				Objects
			</Tabs.Trigger>
			{#if showVersionsTab}
				<Tabs.Trigger value="versions">
					<History class="mr-1.5 h-4 w-4" />
					Versions
				</Tabs.Trigger>
			{/if}
			<Tabs.Trigger value="acl">
				<Shield class="mr-1.5 h-4 w-4" />
				Access Control
			</Tabs.Trigger>
			<Tabs.Trigger value="cors">
				<Globe class="mr-1.5 h-4 w-4" />
				CORS
			</Tabs.Trigger>
			<Tabs.Trigger value="uploads">
				<UploadCloud class="mr-1.5 h-4 w-4" />
				Uploads
			</Tabs.Trigger>
		</Tabs.List>

		<Tabs.Content value="objects" class="space-y-6">
			<!-- Breadcrumbs -->
			<nav class="flex items-center gap-1 text-sm">
				<Folder class="h-4 w-4 text-muted-foreground" />
				{#each breadcrumbs as crumb, i (crumb.prefix)}
					{#if i > 0}
						<span class="text-muted-foreground">/</span>
					{/if}
					{#if i === breadcrumbs.length - 1}
						<span class="font-medium">{crumb.label}</span>
					{:else}
						<button
							class="text-primary underline-offset-4 hover:underline"
							onclick={() => navigatePrefix(crumb.prefix)}
						>
							{crumb.label}
						</button>
					{/if}
				{/each}
			</nav>

			<!-- Object Browser -->
			<BucketObjectBrowser
				{bucket}
				{prefix}
				{totalObjects}
				bind:uploadOpen
				bind:createFolderOpen
				onnavigate={navigatePrefix}
			/>
		</Tabs.Content>

		{#if showVersionsTab}
			<Tabs.Content value="versions" class="space-y-4">
				<BucketVersions {bucket} versioningStatus={versioning.status} />
			</Tabs.Content>
		{/if}

		<Tabs.Content value="acl">
			<BucketAcl {bucket} {tenant} />
		</Tabs.Content>

		<Tabs.Content value="cors">
			<BucketCors {bucket} />
		</Tabs.Content>

		<Tabs.Content value="uploads">
			<BucketUploads {bucket} />
		</Tabs.Content>
	</Tabs.Root>
</div>
