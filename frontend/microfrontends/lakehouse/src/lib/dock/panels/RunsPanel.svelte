<script lang="ts">
	/**
	 * Recent runs from the shared `LineageState`.
	 *
	 * Its contribution to the drag test is SCROLL OFFSET: a remount would scroll a long run list back
	 * to the top, which is the least dramatic of the four failures and the easiest to miss in review.
	 */
	import { Badge } from '@rask/ui/badge';
	import { getLineageState } from '$lib/dock/lineage-context';

	const store = getLineageState();

	// Newest first. The store hands back whatever the feed ordered; a run board reads backwards.
	const runs = $derived(
		[...store.runs].sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? '')),
	);

	function tone(state: string | null | undefined): 'success' | 'destructive' | 'secondary' {
		if (state === null || state === undefined) return 'secondary';
		if (/COMPLETE|SUCCESS/i.test(state)) return 'success';
		if (/FAIL|ABORT/i.test(state)) return 'destructive';
		return 'secondary';
	}
</script>

<div class="scroll">
	{#if runs.length === 0}
		<p class="empty">No runs yet — they appear as pipelines emit OpenLineage events.</p>
	{:else}
		<ul>
			{#each runs as run (run.run_id)}
				<li>
					<Badge variant={tone(run.state)}>{run.state ?? 'unknown'}</Badge>
					<span class="job" title={run.job ?? ''}>{run.job ?? run.run_id}</span>
					<span class="ts">{run.updated_at ?? ''}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.scroll {
		height: 100%;
		overflow-y: auto;
	}
	.empty {
		margin: 0;
		padding: 16px;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	ul {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	li {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 12px;
		border-bottom: 1px solid var(--border);
		font-size: 12px;
	}
	.job {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ts {
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
</style>
