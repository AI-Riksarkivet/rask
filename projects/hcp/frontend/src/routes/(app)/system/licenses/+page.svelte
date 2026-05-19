<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import { formatBytes } from '$lib/utils/format.js';
	import { get_licenses, type License } from '$lib/remote/system.remote.js';

	let licensesData = $derived(get_licenses({}));
	let licenses = $derived((licensesData?.current ?? []) as License[]);

	function isExpired(date?: string): boolean {
		if (!date) return false;
		return new Date(date) < new Date();
	}

	function isExpiringSoon(date?: string): boolean {
		if (!date) return false;
		const d = new Date(date);
		const thirtyDaysFromNow = new Date();
		thirtyDaysFromNow.setDate(thirtyDaysFromNow.getDate() + 30);
		return d > new Date() && d <= thirtyDaysFromNow;
	}

	function formatDate(date?: string): string {
		if (!date) return '—';
		try {
			return new Date(date).toLocaleDateString(undefined, {
				year: 'numeric',
				month: 'short',
				day: 'numeric',
			});
		} catch {
			return date;
		}
	}
</script>

<svelte:head>
	<title>Licenses - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="License Management"
		description="View current storage licenses and their status"
	/>

	{#await licensesData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>Storage Licenses</Card.Title>
				<Card.Description>
					{licenses.length} license{licenses.length !== 1 ? 's' : ''} installed
				</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if licenses.length === 0}
					<p class="py-8 text-center text-sm text-muted-foreground">No licenses found.</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Serial Number</Table.Head>
								<Table.Head>Feature</Table.Head>
								<Table.Head class="text-right">Local Capacity</Table.Head>
								<Table.Head class="text-right">Extended Capacity</Table.Head>
								<Table.Head>Upload Date</Table.Head>
								<Table.Head>Expiration</Table.Head>
								<Table.Head>Status</Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each licenses as lic (lic.serialNumber)}
								<Table.Row>
									<Table.Cell class="font-mono text-sm">{lic.serialNumber ?? '—'}</Table.Cell>
									<Table.Cell>{lic.feature ?? '—'}</Table.Cell>
									<Table.Cell class="text-right">
										{lic.localCapacity ? formatBytes(lic.localCapacity) : '—'}
									</Table.Cell>
									<Table.Cell class="text-right">
										{lic.extendedCapacity ? formatBytes(lic.extendedCapacity) : '—'}
									</Table.Cell>
									<Table.Cell>{formatDate(lic.uploadDate)}</Table.Cell>
									<Table.Cell>{formatDate(lic.expirationDate)}</Table.Cell>
									<Table.Cell>
										{#if isExpired(lic.expirationDate)}
											<Badge variant="destructive">Expired</Badge>
										{:else if isExpiringSoon(lic.expirationDate)}
											<Badge variant="outline" class="border-yellow-500 text-yellow-600">
												Expiring Soon
											</Badge>
										{:else}
											<Badge variant="secondary">Active</Badge>
										{/if}
									</Table.Cell>
								</Table.Row>
							{/each}
						</Table.Body>
					</Table.Root>
				{/if}
			</Card.Content>
		</Card.Root>
	{/await}
</div>
