<script lang="ts">
	/** Compare — the A/B node, and the only one that reads EVERY incoming payload rather than the
	 *  first. Wire two Model nodes into it (gemma on one, qwen on the other) and it reports both
	 *  outputs side by side with a verdict and the first differing line. That comparison is the
	 *  reason a model sandbox exists, so it is a node rather than a panel: the report travels
	 *  downstream like any other payload and can be inspected, copied or fed onward. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
	const inputs = $derived(graph.edges.filter((e) => e.target === id).length);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Compare" status={rt.status} {selected}>
		<p class="text-muted-foreground text-[0.65rem]">
			Wire two or more branches in — every input is compared, A/B/C in edge order.
		</p>
		<p class="mt-1 text-[0.7rem]">
			<span class="text-foreground font-medium">{inputs}</span>
			<span class="text-muted-foreground">input{inputs === 1 ? '' : 's'} wired</span>
			{#if inputs === 1}
				<span class="text-amber-600 dark:text-amber-400"> — comparing one thing does nothing</span>
			{/if}
		</p>
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
