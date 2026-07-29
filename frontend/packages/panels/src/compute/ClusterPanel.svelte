<script lang="ts">
	/** Cluster capacity — nodes alive and the CPU/GPU/memory triplet, used against total. */
	import { getComputeReads } from './context.js';

	const cluster = getComputeReads().getRayCluster();
	const c = $derived(cluster.current ?? null);

	const rows = $derived.by(() => {
		const total = c?.total_resources;
		const used = c?.used_resources;
		if (!total || !used) return [];
		return [
			{ label: 'CPU', used: used.CPU, total: total.CPU, unit: '' },
			{ label: 'GPU', used: used.GPU, total: total.GPU, unit: '' },
			{
				label: 'Memory',
				used: Math.round(used.memory / 1e9),
				total: Math.round(total.memory / 1e9),
				unit: ' GB',
			},
		];
	});

	/** Guard the divide: an idle cluster can legitimately report a total of 0 for a resource. */
	const pct = (used: number, total: number): number =>
		total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
</script>

<div class="scroll">
	{#if c === null}
		<p class="empty">Cluster unavailable.</p>
	{:else}
		<p class="nodes">
			<strong>{c.alive_count ?? 0}</strong> of {c.node_count ?? 0} nodes alive
		</p>
		{#each rows as row (row.label)}
			<div class="row">
				<span class="label">{row.label}</span>
				<div class="bar"><div class="fill" style:width={`${pct(row.used, row.total)}%`}></div></div>
				<span class="num">{row.used}{row.unit} / {row.total}{row.unit}</span>
			</div>
		{/each}
	{/if}
</div>

<style>
	.scroll {
		height: 100%;
		overflow-y: auto;
		padding: 12px;
	}
	.empty {
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.nodes {
		margin: 0 0 12px;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.row {
		display: grid;
		grid-template-columns: 4rem 1fr auto;
		align-items: center;
		gap: 8px;
		margin-bottom: 8px;
		font-size: 12px;
	}
	.label {
		color: var(--muted-foreground);
	}
	.bar {
		height: 6px;
		border-radius: 999px;
		background: var(--muted);
		overflow: hidden;
	}
	.fill {
		height: 100%;
		background: var(--primary);
		transition: width 0.2s ease;
	}
	.num {
		font-variant-numeric: tabular-nums;
		color: var(--muted-foreground);
		white-space: nowrap;
	}
</style>
