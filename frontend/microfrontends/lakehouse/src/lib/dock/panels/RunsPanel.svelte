<script lang="ts">
	/** Recent runs from the shared `LineageState` — newest first, as the run board reads them. */
	import { Badge } from '@rask/ui/badge';
	import { getBench } from '$lib/dock/bench.svelte';

	const store = getBench();
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

<div class="h-full min-h-0 overflow-auto">
	{#if !store.settled}
		<p class="text-muted-foreground p-3 text-xs">Connecting to the lineage feed…</p>
	{:else if runs.length === 0}
		<p class="text-muted-foreground p-3 text-xs">
			No runs yet — they appear as pipelines emit OpenLineage events.
		</p>
	{:else}
		<ul class="m-0 list-none p-0">
			{#each runs as run (run.run_id)}
				<li class="border-border/60 flex items-center gap-2 border-b px-3 py-1.5 text-xs">
					<Badge variant={tone(run.state)}>{run.state ?? 'unknown'}</Badge>
					<span class="min-w-0 flex-1 truncate font-mono" title={run.job ?? ''}
						>{run.job ?? run.run_id}</span
					>
					<span class="text-muted-foreground text-[11px]">{run.updated_at ?? ''}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>
