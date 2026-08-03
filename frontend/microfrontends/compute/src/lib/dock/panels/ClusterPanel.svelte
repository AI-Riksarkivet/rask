<script lang="ts">
	/** Ray nodes — the zone's own `getRayCluster` remote, same alive/head badges as /compute/cluster. */
	import { onMount } from 'svelte';
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { getRayCluster } from '$lib/remote/compute.remote';

	const clusterQuery = getRayCluster();
	const nodes = $derived(clusterQuery.current?.nodes ?? []);

	// POLL REASON: snapshot-only Ray introspection; no cursor exists to ride.
	onMount(() => {
		const timer = setInterval(() => {
			clusterQuery.refresh().catch(() => {});
		}, 5000);
		return () => clearInterval(timer);
	});

	const pct = (used: number | null, total: number | null): string =>
		used === null || total === null || total === 0 ? '—' : `${Math.round((used / total) * 100)}%`;
</script>

<div class="bg-background h-full min-h-0 overflow-auto p-3">
	{#if nodes.length === 0}
		<p class="text-muted-foreground text-sm">No nodes reported.</p>
	{:else}
		<Card class="overflow-hidden">
			<table class="w-full border-collapse text-xs">
				<thead class="bg-card sticky top-0 text-left">
					<tr class="border-b">
						<th class="px-3 py-2">node</th>
						<th class="px-3 py-2">role</th>
						<th class="px-3 py-2">state</th>
						<th class="px-3 py-2">cpu</th>
						<th class="px-3 py-2">mem</th>
						<th class="px-3 py-2">gpus</th>
					</tr>
				</thead>
				<tbody>
					{#each nodes as node (node.node_id)}
						<tr class="border-border/40 border-b">
							<td class="px-3 py-1.5 font-mono">{node.hostname ?? node.node_ip ?? '—'}</td>
							<td class="px-3 py-1.5">{node.is_head ? 'head' : 'worker'}</td>
							<td class="px-3 py-1.5">
								<Badge variant={node.alive ? 'success' : 'destructive'}
									>{node.alive ? 'alive' : 'dead'}</Badge
								>
							</td>
							<td class="px-3 py-1.5 tabular-nums">
								{node.host_cpu_percent === null ? '—' : `${Math.round(node.host_cpu_percent)}%`}
							</td>
							<td class="px-3 py-1.5 tabular-nums">{pct(node.host_mem_used, node.host_mem_total)}</td>
							<td class="px-3 py-1.5 tabular-nums">{node.gpus.length}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</Card>
	{/if}
</div>
