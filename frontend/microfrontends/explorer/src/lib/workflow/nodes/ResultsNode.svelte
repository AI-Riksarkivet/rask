<script lang="ts">
	/** Results node: renders the Hit[] handed down its incoming edge. Click a row
	 *  to play it in the Inspector. Forwards its hits unchanged downstream, so
	 *  Results → Export (or → Tagger) chains work. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/workflow/graph.svelte';
	import HitList from '$lib/workflow/HitList.svelte';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const rt = $derived(graph.runtime[id]);
	const hits = $derived(rt?.hits ?? []);
</script>

{#if rt}
	<NodeShell {id} title="Results" status={rt.status} {selected} width="w-80">
		<Handle type="target" position={Position.Left} />
		{#if rt.status === 'idle'}
			<p class="text-muted-foreground text-xs">Run the graph to see hits here.</p>
		{:else if hits.length === 0}
			<p class="text-muted-foreground text-xs">No results.</p>
		{:else}
			<div class="text-muted-foreground mb-1.5 text-[0.7rem]">
				<span class="text-foreground">{hits.length}</span> results · click to play
			</div>
			<HitList {hits} />
		{/if}
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
