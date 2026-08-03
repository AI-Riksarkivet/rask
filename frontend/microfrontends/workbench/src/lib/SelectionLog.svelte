<script lang="ts">
	/**
	 * A NATIVE compositor panel: the live log of what foreign panels selected. Exists for two
	 * reasons — it is genuinely useful (the workbench's own surface for "what did I just click,
	 * where"), and it is the spike's cross-boundary proof: an event dispatched inside
	 * `<rask-compute-jobs>` must land here, through the valibot relay, with no shared store.
	 */
	import { getSelections } from './selections.svelte';

	// A PanelProps component that needs none of them — the registry accepts a props-less panel
	// (AnyPanelComponent), so nothing is destructured here.
	const selections = getSelections();
</script>

<div class="log">
	{#if selections.rows.length === 0}
		<p class="dim">Nothing selected yet — click a row in a foreign panel.</p>
	{:else}
		<ul>
			{#each selections.rows as row, i (i)}
				<li>
					<span class="kind">{row.kind}</span>
					<span class="label">{row.label}</span>
					<span class="dim">from {row.source}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.log {
		height: 100%;
		overflow: auto;
		padding: 0.75rem;
		font-size: 0.8125rem;
	}
	ul {
		margin: 0;
		padding: 0;
		list-style: none;
		display: grid;
		gap: 0.375rem;
	}
	li {
		display: flex;
		gap: 0.5rem;
		align-items: baseline;
		border-bottom: 1px solid var(--color-border);
		padding-bottom: 0.375rem;
	}
	.kind {
		border: 1px solid var(--color-border);
		border-radius: 0.25rem;
		padding: 0 0.375rem;
		font-size: 0.6875rem;
		color: var(--color-muted-foreground);
	}
	.label {
		font-family: var(--font-mono, monospace);
	}
	.dim {
		color: var(--color-muted-foreground);
	}
</style>
