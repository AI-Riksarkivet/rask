<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Progress } from '$lib/components/ui/progress/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import { formatBytes } from '$lib/utils/format.js';
	import {
		get_node_statistics,
		type NodeStatistics,
		type NodeStats,
	} from '$lib/remote/system.remote.js';

	let statsData = $derived(get_node_statistics({}));
	let stats = $derived((statsData?.current ?? { nodes: [] }) as NodeStatistics);
	let nodes = $derived(stats.nodes ?? []);

	function cpuPercent(node: NodeStats): number {
		return Math.round((node.cpuUser ?? 0) + (node.cpuSystem ?? 0));
	}

	function connPercent(current: number | undefined, max: number | undefined): number {
		if (!current || !max || max === 0) return 0;
		return Math.round((current / max) * 100);
	}
</script>

<svelte:head>
	<title>Nodes - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Node Statistics"
		description="View per-node CPU, network, and volume statistics"
	/>

	{#await statsData}
		<div class="grid gap-6 md:grid-cols-2">
			<CardSkeleton />
			<CardSkeleton />
		</div>
	{:then}
		{#if nodes.length === 0}
			<Card.Root>
				<Card.Content class="py-8 text-center text-sm text-muted-foreground">
					No node statistics available.
				</Card.Content>
			</Card.Root>
		{:else}
			<div class="grid gap-6 md:grid-cols-2">
				{#each nodes as node (node.nodeNumber)}
					<Card.Root>
						<Card.Header>
							<div class="flex items-center justify-between">
								<Card.Title>Node {node.nodeNumber}</Card.Title>
								<Badge variant="secondary">{node.frontendIpAddresses?.[0] ?? '—'}</Badge>
							</div>
							<Card.Description>
								Backend: {node.backendIpAddress ?? '—'}
							</Card.Description>
						</Card.Header>
						<Card.Content class="space-y-4">
							<div class="space-y-2">
								<div class="flex justify-between text-sm">
									<span>CPU Usage</span>
									<span class="text-muted-foreground">{cpuPercent(node)}%</span>
								</div>
								<Progress value={cpuPercent(node)} max={100} />
							</div>

							<div class="grid grid-cols-2 gap-4 text-sm">
								<div>
									<p class="text-muted-foreground">HTTP Connections</p>
									<p class="font-medium">
										{node.openHttpConnections ?? 0} / {node.maxHttpConnections ?? 0}
									</p>
								</div>
								<div>
									<p class="text-muted-foreground">HTTPS Connections</p>
									<p class="font-medium">
										{node.openHttpsConnections ?? 0} / {node.maxHttpsConnections ?? 0}
									</p>
								</div>
								<div>
									<p class="text-muted-foreground">Frontend Read</p>
									<p class="font-medium">{formatBytes(node.frontEndBytesRead ?? 0)}/s</p>
								</div>
								<div>
									<p class="text-muted-foreground">Frontend Write</p>
									<p class="font-medium">{formatBytes(node.frontEndBytesWritten ?? 0)}/s</p>
								</div>
								<div>
									<p class="text-muted-foreground">I/O Wait</p>
									<p class="font-medium">{node.ioWait ?? 0}%</p>
								</div>
								<div>
									<p class="text-muted-foreground">Swap Out</p>
									<p class="font-medium">{node.swapOut ?? 0}</p>
								</div>
							</div>

							{#if node.volumes && node.volumes.length > 0}
								<div class="space-y-2">
									<p class="text-sm font-medium">Volumes</p>
									{#each node.volumes as vol (vol.id)}
										<div class="rounded border p-3 text-sm">
											<div class="mb-1 flex justify-between">
												<span class="font-mono text-xs">{vol.id}</span>
												<span class="text-muted-foreground"
													>{vol.diskUtilization?.toFixed(1)}% used</span
												>
											</div>
											<Progress value={vol.diskUtilization ?? 0} max={100} />
											<div class="mt-1 flex justify-between text-xs text-muted-foreground">
												<span>{formatBytes((vol.totalBytes ?? 0) - (vol.freeBytes ?? 0))} used</span
												>
												<span>{formatBytes(vol.totalBytes ?? 0)} total</span>
											</div>
										</div>
									{/each}
								</div>
							{/if}
						</Card.Content>
					</Card.Root>
				{/each}
			</div>
		{/if}
	{/await}
</div>
