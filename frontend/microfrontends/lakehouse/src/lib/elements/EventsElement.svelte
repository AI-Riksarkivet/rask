<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-events>` — the OpenLineage event feed, served by the lakehouse zone to the
	 * global workbench. Shares the module-singleton store with every other lakehouse element on
	 * the page (one poller). Keyed on `seq`, the feed's own monotonic id.
	 */
	import { ensurePolling, lineage } from './store';

	$effect(() => {
		ensurePolling();
	});

	const events = $derived([...lineage.events].reverse());
</script>

<div class="events">
	{#if !lineage.settled}
		<p class="empty">Connecting to the lineage feed…</p>
	{:else if !lineage.online}
		<p class="empty">
			Lineage feed unavailable — your session may have expired or lack access. Sign out and back in; if
			it persists, ask an admin for lineage access.
		</p>
	{:else if events.length === 0}
		<p class="empty">No events in the window.</p>
	{:else}
		<ul>
			{#each events as ev (ev.seq)}
				<li>
					<span class="type">{ev.event_type ?? '—'}</span>
					<span class="job" title={ev.job ?? ''}>{ev.job ?? ''}</span>
					<span class="ts">{ev.event_time ?? ''}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.events {
		display: block;
		height: 100%;
		overflow-y: auto;
	}
	.empty {
		margin: 0;
		padding: 1rem;
		font-size: 0.75rem;
		color: var(--color-muted-foreground);
	}
	ul {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	li {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.375rem 0.75rem;
		border-bottom: 1px solid var(--color-border);
		font-size: 0.8125rem;
	}
	.type {
		border: 1px solid var(--color-border);
		border-radius: 0.25rem;
		padding: 0 0.375rem;
		font-size: 0.6875rem;
		color: var(--color-muted-foreground);
	}
	.job {
		flex: 1 1 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono, monospace);
	}
	.ts {
		color: var(--color-muted-foreground);
		font-size: 0.6875rem;
	}
</style>
