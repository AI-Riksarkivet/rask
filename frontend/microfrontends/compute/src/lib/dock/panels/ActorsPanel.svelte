<script lang="ts">
	/** Live Ray actors — the zone's own `getActors` remote. */
	import { onMount } from 'svelte';
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { getActors } from '$lib/remote/compute.remote';

	const actorsQuery = getActors();
	const actors = $derived(actorsQuery.current ?? []);

	// POLL REASON: snapshot-only Ray introspection; no cursor exists to ride.
	onMount(() => {
		const timer = setInterval(() => {
			actorsQuery.refresh().catch(() => {});
		}, 5000);
		return () => clearInterval(timer);
	});
</script>

<div class="bg-background h-full min-h-0 overflow-auto p-3">
	{#if actors.length === 0}
		<p class="text-muted-foreground text-sm">No actors.</p>
	{:else}
		<Card class="overflow-hidden">
			<table class="w-full border-collapse text-xs">
				<thead class="bg-card sticky top-0 text-left">
					<tr class="border-b">
						<th class="px-3 py-2">actor</th>
						<th class="px-3 py-2">state</th>
						<th class="px-3 py-2">pid</th>
						<th class="px-3 py-2">restarts</th>
					</tr>
				</thead>
				<tbody>
					{#each actors as actor (actor.actor_id ?? actor.class_name)}
						<tr class="border-border/40 border-b">
							<td class="px-3 py-1.5 font-mono">{actor.name ?? actor.class_name}</td>
							<td class="px-3 py-1.5">
								<Badge variant={actor.state === 'ALIVE' ? 'success' : 'secondary'}>{actor.state}</Badge>
							</td>
							<td class="px-3 py-1.5 tabular-nums">{actor.pid ?? '—'}</td>
							<td class="px-3 py-1.5 tabular-nums">{actor.num_restarts}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</Card>
	{/if}
</div>
