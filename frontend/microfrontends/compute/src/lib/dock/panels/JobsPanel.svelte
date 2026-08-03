<script lang="ts">
	/**
	 * Ray jobs — the zone's OWN remote query, polled on the zone's own clock, in the same Card table
	 * the /compute/jobs page renders. A panel inside its zone can do this; the retired cross-zone
	 * element could not (remote functions are per-app).
	 */
	import { onMount } from 'svelte';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { getRayJobs } from '$lib/remote/compute.remote';

	const jobsQuery = getRayJobs();
	const jobs = $derived(jobsQuery.current?.jobs ?? []);

	// POLL REASON: the Ray dashboard REST API is snapshot-only introspection — Ray publishes no
	// change events a cursor could ride. `.catch(() => {})` is mandatory: one uncaught rejection
	// evicts the query from cache and kills the loop.
	onMount(() => {
		const timer = setInterval(() => {
			jobsQuery.refresh().catch(() => {});
		}, 5000);
		return () => clearInterval(timer);
	});

	function variantFor(s: string): BadgeVariant {
		switch (s) {
			case 'SUCCEEDED':
				return 'success';
			case 'RUNNING':
			case 'PENDING':
				return 'warning';
			case 'FAILED':
				return 'destructive';
			default:
				return 'secondary';
		}
	}
	const fmtTime = (ts: number | null): string =>
		ts ? new Date(ts).toISOString().replace('T', ' ').slice(0, 19) : '—';
</script>

<div class="bg-background h-full min-h-0 overflow-auto p-3">
	{#if jobs.length === 0}
		<p class="text-muted-foreground text-sm">No job submissions.</p>
	{:else}
		<Card class="overflow-hidden">
			<table class="w-full border-collapse text-xs">
				<thead class="bg-card sticky top-0 text-left">
					<tr class="border-b">
						<th class="px-3 py-2">status</th>
						<th class="px-3 py-2">submission_id</th>
						<th class="px-3 py-2">started</th>
						<th class="px-3 py-2">entrypoint</th>
					</tr>
				</thead>
				<tbody>
					{#each jobs as j (j.submission_id)}
						<tr class="border-border/40 border-b">
							<td class="px-3 py-1.5"><Badge variant={variantFor(j.status)}>{j.status}</Badge></td>
							<td class="px-3 py-1.5 font-mono">{j.submission_id}</td>
							<td class="text-muted-foreground px-3 py-1.5 font-mono">{fmtTime(j.start_time)}</td>
							<td class="text-muted-foreground px-3 py-1.5 font-mono">{j.entrypoint ?? '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</Card>
	{/if}
</div>
