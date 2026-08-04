<script lang="ts">
	/**
	 * Ray Serve applications and their deployments — the zone's own `getServe` remote.
	 *
	 * Panel-sized on purpose, matching its three siblings: the /serve PAGE is the place for proxies,
	 * controller metrics and per-replica detail, while a dock panel answers the question you keep a
	 * dock open for — "is transcribe still up, and how many replicas". Same remote, same query cache,
	 * so the two can never disagree about what Serve is doing.
	 */
	import { onMount } from 'svelte';
	import { type ServeApplication } from '@rask/api';
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { getServe } from '$lib/remote/compute.remote';

	const serveQuery = getServe();
	const payload = $derived(serveQuery.current ?? null);
	const apps = $derived.by<ServeApplication[]>(() =>
		Object.values(payload?.applications ?? {}).sort((a, b) => a.name.localeCompare(b.name)),
	);

	// POLL REASON: the Ray dashboard REST API is snapshot-only introspection — Ray publishes no change
	// event a cursor could ride. `.catch(() => {})` is MANDATORY: one uncaught refresh rejection evicts
	// the query from cache and silently kills the loop (compute.remote.ts:25-40).
	onMount(() => {
		const timer = setInterval(() => {
			serveQuery.refresh().catch(() => {});
		}, 5000);
		return () => clearInterval(timer);
	});
</script>

<div class="bg-background h-full min-h-0 overflow-auto p-3">
	{#if apps.length === 0}
		<p class="text-muted-foreground text-sm">No Serve applications.</p>
	{:else}
		<Card class="overflow-hidden">
			<table class="w-full border-collapse text-xs">
				<thead class="bg-card sticky top-0 text-left">
					<tr class="border-b">
						<th class="px-3 py-2">application</th>
						<th class="px-3 py-2">status</th>
						<th class="px-3 py-2">deployments</th>
						<th class="px-3 py-2">replicas</th>
					</tr>
				</thead>
				<tbody>
					{#each apps as app (app.name)}
						{@const deployments = Object.values(app.deployments ?? {})}
						{@const replicas = deployments.reduce((n, d) => n + (d.replicas?.length ?? 0), 0)}
						<tr class="border-border/40 border-b">
							<td class="px-3 py-1.5 font-mono">{app.name}</td>
							<td class="px-3 py-1.5">
								<Badge variant={app.status === 'RUNNING' ? 'success' : 'secondary'}>
									{app.status}
								</Badge>
							</td>
							<td class="px-3 py-1.5 tabular-nums">
								{deployments.filter((d) => d.status === 'HEALTHY').length}/{deployments.length}
							</td>
							<td class="px-3 py-1.5 tabular-nums">{replicas}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</Card>
	{/if}
</div>
