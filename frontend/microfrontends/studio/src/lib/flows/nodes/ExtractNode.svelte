<script lang="ts">
	/** Extract JSON — pull one field out of a model's response by dotted path.
	 *
	 *  Small node, load-bearing: a Serve app answers an ENVELOPE (`{"choices":[{"message":{...}}]}`),
	 *  and without this everything downstream sees the envelope instead of the answer. It is what
	 *  makes model → model chaining possible at all. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Extract JSON" status={rt.status} {selected}>
		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="path-{id}">Path</label>
		<input
			id="path-{id}"
			class="{FIELD_CLASS} w-full font-mono"
			placeholder="choices[0].message.content"
			bind:value={cfg.extractPath}
		/>
		<p class="text-muted-foreground mt-1 text-[0.65rem]">Empty path emits the whole document.</p>
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
