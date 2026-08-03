<script lang="ts">
	/** Combine node: explicitly union or intersect its incoming result sets, so
	 *  fan-in is unambiguous (vs the implicit union when wiring straight into a
	 *  Search/Results). Takes 2+ result-set inputs, emits one merged set. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph, type CombineMode } from '$lib/workflow/graph.svelte';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);

	const MODES: { value: CombineMode; label: string }[] = [
		{ value: 'union', label: '∪ Union' },
		{ value: 'intersect', label: '∩ Intersect' },
	];
</script>

{#if cfg && rt}
	<NodeShell {id} title="Combine" status={rt.status} {selected}>
		<Handle type="target" position={Position.Left} />

		<div class="flex gap-1">
			{#each MODES as m (m.value)}
				<button
					type="button"
					onclick={() => graph.setConfig(id, { combineMode: m.value })}
					class="nodrag flex-1 rounded-md border px-2 py-1 text-xs transition-colors {cfg.combineMode ===
					m.value
						? 'border-primary bg-primary/10 text-foreground'
						: 'border-border text-muted-foreground hover:bg-muted'}"
				>
					{m.label}
				</button>
			{/each}
		</div>
		<p class="text-muted-foreground mt-1 text-[0.7rem]">
			{cfg.combineMode === 'intersect'
				? 'Keep only chunks present in ALL inputs.'
				: 'Keep chunks present in ANY input.'}
		</p>

		<!-- Run summary; hidden on error so the NodeShell banner stands alone. -->
		{#if rt.status !== 'error'}
			<div class="border-border mt-2 border-t pt-1.5 text-[0.7rem]">
				{#if rt.status === 'done'}
					<span class="text-muted-foreground"
						><span class="text-foreground">{rt.count}</span> combined</span
					>
				{:else}
					<span class="text-muted-foreground/70">idle — wire 2+ result sets in, then Run</span>
				{/if}
			</div>
		{/if}

		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
