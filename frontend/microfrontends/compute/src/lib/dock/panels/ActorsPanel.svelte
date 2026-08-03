<script lang="ts">
	/** Live Ray actors — the long-lived workers the HTR pipeline packs onto the GPUs. */
	import { Badge } from '@rask/ui/badge';
	import { getActors } from '$lib/remote/compute.remote';

	const actors = getActors();
	const rows = $derived(actors.current ?? []);

	function tone(state: string): 'success' | 'destructive' | 'secondary' {
		if (/ALIVE/i.test(state)) return 'success';
		if (/DEAD/i.test(state)) return 'destructive';
		return 'secondary';
	}
</script>

<div class="scroll">
	{#if rows.length === 0}
		<p class="empty">No actors.</p>
	{:else}
		<ul>
			<!-- `actor_id` is nullable in the schema; fall back to the index-free composite so the key is
			     still stable across a refresh that reorders. -->
			{#each rows as actor (actor.actor_id ?? `${actor.class_name}:${actor.pid ?? 'n'}`)}
				<li>
					<Badge variant={tone(actor.state)}>{actor.state}</Badge>
					<span class="cls">{actor.class_name}</span>
					{#if actor.num_restarts > 0}<span class="restarts">↻{actor.num_restarts}</span>{/if}
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
	.cls {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.restarts {
		color: var(--warning);
		font-variant-numeric: tabular-nums;
	}
</style>
