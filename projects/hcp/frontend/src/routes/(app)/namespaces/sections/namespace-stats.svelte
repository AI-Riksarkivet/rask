<script lang="ts">
	import { FileBox, HardDrive, Boxes, Users, ChartPie } from 'lucide-svelte';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import StatCard from '$lib/components/custom/stat-card/stat-card.svelte';
	import StorageProgressBar from '$lib/components/custom/storage-progress-bar/storage-progress-bar.svelte';
	import type { RemoteQuery } from '@sveltejs/kit';
	import {
		get_tenant,
		get_tenant_statistics,
		type ChargebackReport,
	} from '$lib/remote/tenant-info.remote.js';
	import { type Namespace } from '$lib/remote/namespaces.remote.js';
	import { get_users } from '$lib/remote/users.remote.js';
	import {
		formatBytes,
		parseQuotaBytes,
		buildStorageMap,
		calcQuotaPercent,
		type ChargebackEntry,
	} from '$lib/utils/format.js';

	let {
		tenant,
		nsData,
		chargebackData,
	}: {
		tenant: string;
		nsData: RemoteQuery<Namespace[]>;
		chargebackData: RemoteQuery<ChargebackReport>;
	} = $props();

	let tenantInfo = $derived(get_tenant({ tenant }));
	let tenantStats = $derived(get_tenant_statistics({ tenant }));
	let usersData = $derived(get_users({ tenant }));

	let chargeback = $derived((chargebackData?.current?.chargebackData ?? []) as ChargebackEntry[]);
	let users = $derived((usersData?.current ?? []) as { username: string }[]);
	let namespaces = $derived((nsData?.current ?? []) as Namespace[]);

	let quotaPercent = $derived(
		calcQuotaPercent(
			Number(tenantStats?.current?.storageCapacityUsed ?? 0),
			tenantInfo?.current?.hardQuota
		)
	);

	let totalAllocatedBytes = $derived.by(() => {
		let total = 0;
		for (const ns of namespaces) {
			if (ns.hardQuota) {
				const bytes = parseQuotaBytes(ns.hardQuota);
				if (bytes) total += bytes;
			}
		}
		return total;
	});

	let allocatedPercent = $derived(
		calcQuotaPercent(totalAllocatedBytes, tenantInfo?.current?.hardQuota)
	);
</script>

<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
	{#await tenantStats}
		<CardSkeleton />
		<CardSkeleton />
		<CardSkeleton />
		<CardSkeleton />
		<CardSkeleton />
	{:then stats}
		<StatCard label="Objects" value={(stats?.objectCount ?? 0).toLocaleString()} icon={FileBox}>
			<p class="mt-1 text-xs text-muted-foreground">
				{stats?.customMetadataCount ?? 0} with custom metadata
			</p>
		</StatCard>

		<StatCard
			label="Storage Used"
			value={formatBytes(Number(stats?.storageCapacityUsed ?? 0))}
			icon={HardDrive}
			delay="delay-75"
		>
			{#await tenantInfo then info}
				{#if quotaPercent !== null}
					<StorageProgressBar percent={quotaPercent} class="mt-2" />
					<p class="mt-1 text-xs text-muted-foreground">
						{formatBytes(Number(stats?.storageCapacityUsed ?? 0))} / {info?.hardQuota}
					</p>
				{:else}
					<p class="mt-1 text-xs text-muted-foreground">No quota limit</p>
				{/if}
			{/await}
		</StatCard>

		<StatCard
			label="Quota Allocated"
			value={formatBytes(totalAllocatedBytes)}
			icon={ChartPie}
			delay="delay-150"
		>
			{#await tenantInfo then info}
				{#if allocatedPercent !== null}
					<StorageProgressBar percent={allocatedPercent} class="mt-2" />
					<p class="mt-1 text-xs text-muted-foreground">
						{formatBytes(totalAllocatedBytes)} / {info?.hardQuota}
					</p>
				{:else}
					<p class="mt-1 text-xs text-muted-foreground">
						{namespaces.filter((n) => n.hardQuota).length} of {namespaces.length} with quotas
					</p>
				{/if}
			{/await}
		</StatCard>

		{#await nsData}
			<CardSkeleton />
		{:then}
			<StatCard
				label="Namespaces"
				value={String(namespaces.length)}
				icon={Boxes}
				delay="delay-200"
			/>
		{/await}

		<div class="animate-in fade-in slide-in-from-bottom-2 h-full duration-300 delay-300">
			{#await usersData}
				<CardSkeleton />
			{:then}
				<StatCard label="Users" value={String(users.length)} icon={Users}>
					<p class="mt-1 text-xs">
						<a href="/users" class="text-primary underline-offset-4 hover:underline">
							Manage &rarr;
						</a>
					</p>
				</StatCard>
			{/await}
		</div>
	{/await}
</div>
