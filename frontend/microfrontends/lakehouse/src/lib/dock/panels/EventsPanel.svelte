<script lang="ts">
	/** The OpenLineage event feed from the shared store, newest first. */
	import { getBench } from '$lib/dock/bench.svelte';

	const store = getBench();
	const events = $derived([...store.events].reverse());
</script>

<div class="h-full min-h-0 overflow-auto">
	{#if !store.settled}
		<p class="text-muted-foreground p-3 text-xs">Connecting to the lineage feed…</p>
	{:else if events.length === 0}
		<p class="text-muted-foreground p-3 text-xs">No events in the window.</p>
	{:else}
		<ul class="m-0 list-none p-0">
			{#each events as ev (ev.seq)}
				<li class="border-border/60 flex items-center gap-2 border-b px-3 py-1.5 text-xs">
					<span class="border-border text-muted-foreground rounded border px-1.5 text-[11px]"
						>{ev.event_type ?? '—'}</span
					>
					<span class="min-w-0 flex-1 truncate font-mono" title={ev.job ?? ''}>{ev.job ?? ''}</span>
					<span class="text-muted-foreground text-[11px]">{ev.event_time ?? ''}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>
