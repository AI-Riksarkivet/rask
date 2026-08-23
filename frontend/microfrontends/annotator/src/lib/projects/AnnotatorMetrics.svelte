<script lang="ts">
	// Who is doing the work, and how it is landing. Derived entirely from the task list the queue
	// already reads — no new endpoint, no stored counters. A metric kept beside the facts it
	// summarises can drift from them, and the drift is silent; this cannot drift because there is
	// nothing to drift from.
	import { Badge } from '@rask/ui/badge';

	import { annotatorMetrics } from './metrics.js';
	import type { TaskDetail } from './types.js';

	let { tasks }: { tasks: TaskDetail[] } = $props();

	const rows = $derived(annotatorMetrics(tasks));

	/** A rate is a rate; nothing submitted is NOT a rate of zero, which would read as "everything
	 *  they did was rejected" — the opposite of the truth. */
	const rate = (r: number | null) => (r === null ? '—' : `${Math.round(r * 100)}%`);
</script>

{#if rows.length > 0}
	<section class="flex flex-col gap-2" data-testid="annotator-metrics">
		<h2 class="text-muted-foreground text-xs font-medium uppercase">Annotators</h2>
		<div class="overflow-x-auto">
			<table class="w-full text-sm">
				<thead>
					<tr class="text-muted-foreground border-b text-xs">
						<th class="py-1 text-left font-medium">Who</th>
						<th class="py-1 text-right font-medium" title="Items they still hold, not yet submitted"
							>Open</th
						>
						<th class="py-1 text-right font-medium">Submitted</th>
						<th class="py-1 text-right font-medium">Accepted</th>
						<th class="py-1 text-right font-medium" title="Sent back for changes">Changes</th>
						<th class="py-1 text-right font-medium">Accept rate</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.who)}
						<tr class="border-b last:border-0" data-testid="metrics-row">
							<td class="py-1 font-mono text-xs">{row.who}</td>
							<td class="py-1 text-right tabular-nums">
								{#if row.open > 0}
									<!-- The number a manager is scanning for: work sitting with someone. -->
									<Badge variant="outline">{row.open}</Badge>
								{:else}
									<span class="text-muted-foreground">0</span>
								{/if}
							</td>
							<td class="py-1 text-right tabular-nums">{row.submitted}</td>
							<td class="py-1 text-right tabular-nums">{row.accepted}</td>
							<td class="py-1 text-right tabular-nums">{row.changesRequested}</td>
							<td class="py-1 text-right tabular-nums" data-testid="accept-rate"
								>{rate(row.acceptRate)}</td
							>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
{/if}
