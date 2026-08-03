<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-runs>` — recent lineage runs, served by the lakehouse zone to the global
	 * workbench. Light DOM; styling is the scoped style block over tokens (plus @rask/ui's Badge,
	 * whose utility classes every zone's Tailwind build generates via the shared ui/dist @source).
	 * Data comes from the module-singleton store — ONE poller no matter how many lakehouse
	 * elements the page mounts.
	 */
	import { Badge } from '@rask/ui/badge';
	import { ensurePolling, lineage } from './store';

	$effect(() => {
		ensurePolling();
	});

	const runs = $derived(
		[...lineage.runs].sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? '')),
	);

	function tone(state: string | null | undefined): 'success' | 'destructive' | 'secondary' {
		if (state === null || state === undefined) return 'secondary';
		if (/COMPLETE|SUCCESS/i.test(state)) return 'success';
		if (/FAIL|ABORT/i.test(state)) return 'destructive';
		return 'secondary';
	}

	function select(node: HTMLElement, runId: string, job: string | null) {
		node.dispatchEvent(
			new CustomEvent('rask:select', {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-runs',
					kind: 'lineage-run',
					id: runId,
					label: job ?? runId,
				},
			}),
		);
	}
</script>

<div class="runs">
	{#if !lineage.settled}
		<p class="empty">Connecting to the lineage feed…</p>
	{:else if runs.length === 0}
		<p class="empty">No runs yet — they appear as pipelines emit OpenLineage events.</p>
	{:else}
		<ul>
			{#each runs as run (run.run_id)}
				<li onclick={(e) => select(e.currentTarget, run.run_id, run.job ?? null)}>
					<Badge variant={tone(run.state)}>{run.state ?? 'unknown'}</Badge>
					<span class="job" title={run.job ?? ''}>{run.job ?? run.run_id}</span>
					<span class="ts">{run.updated_at ?? ''}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.runs {
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
		cursor: pointer;
	}
	li:hover {
		background: var(--color-muted);
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
