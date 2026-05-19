<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import {
		get_service_statistics,
		type ServiceStatistics,
		type ServiceInfo,
	} from '$lib/remote/system.remote.js';

	let statsData = $derived(get_service_statistics({}));
	let stats = $derived((statsData?.current ?? { services: [] }) as ServiceStatistics);
	let services = $derived(stats.services ?? []);

	function stateVariant(state?: string): 'default' | 'secondary' | 'outline' | 'destructive' {
		switch (state) {
			case 'RUNNING':
				return 'default';
			case 'IDLE':
				return 'secondary';
			case 'STOPPED':
				return 'destructive';
			default:
				return 'outline';
		}
	}

	function formatDate(ts?: number): string {
		if (!ts) return '—';
		return new Date(ts).toLocaleString();
	}

	function formatRate(rate?: number): string {
		if (rate == null) return '—';
		if (rate < 1) return rate.toFixed(2);
		return rate.toFixed(1);
	}
</script>

<svelte:head>
	<title>Services - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Service Statistics"
		description="View service state and processing statistics"
	/>

	{#await statsData}
		<CardSkeleton />
	{:then}
		{#if services.length === 0}
			<Card.Root>
				<Card.Content class="py-8 text-center text-sm text-muted-foreground">
					No service statistics available.
				</Card.Content>
			</Card.Root>
		{:else}
			<Card.Root>
				<Card.Header>
					<Card.Title>System Services</Card.Title>
					<Card.Description>
						{services.length} service{services.length !== 1 ? 's' : ''} registered
					</Card.Description>
				</Card.Header>
				<Card.Content>
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Service</Table.Head>
								<Table.Head>State</Table.Head>
								<Table.Head>Performance</Table.Head>
								<Table.Head class="text-right">Examined</Table.Head>
								<Table.Head class="text-right">Examined/s</Table.Head>
								<Table.Head class="text-right">Serviced</Table.Head>
								<Table.Head class="text-right">Serviced/s</Table.Head>
								<Table.Head class="text-right">Unable</Table.Head>
								<Table.Head>Started</Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each services as svc (svc.name)}
								<Table.Row>
									<Table.Cell class="font-medium">{svc.name ?? '—'}</Table.Cell>
									<Table.Cell>
										<Badge variant={stateVariant(svc.state)}>{svc.state ?? '—'}</Badge>
									</Table.Cell>
									<Table.Cell>
										<Badge variant="outline">{svc.performanceLevel ?? '—'}</Badge>
									</Table.Cell>
									<Table.Cell class="text-right font-mono">
										{(svc.objectsExamined ?? 0).toLocaleString()}
									</Table.Cell>
									<Table.Cell class="text-right font-mono">
										{formatRate(svc.objectsExaminedPerSecond)}
									</Table.Cell>
									<Table.Cell class="text-right font-mono">
										{(svc.objectsServiced ?? 0).toLocaleString()}
									</Table.Cell>
									<Table.Cell class="text-right font-mono">
										{formatRate(svc.objectsServicedPerSecond)}
									</Table.Cell>
									<Table.Cell class="text-right font-mono">
										{(svc.objectsUnableToService ?? 0).toLocaleString()}
									</Table.Cell>
									<Table.Cell class="text-xs text-muted-foreground">
										{svc.formattedStartTime ?? formatDate(svc.startTime)}
									</Table.Cell>
								</Table.Row>
							{/each}
						</Table.Body>
					</Table.Root>
				</Card.Content>
			</Card.Root>
		{/if}
	{/await}
</div>
