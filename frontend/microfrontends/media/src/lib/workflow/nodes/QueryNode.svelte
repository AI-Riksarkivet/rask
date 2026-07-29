<script lang="ts">
	/** Text-query input. Emits `{ q }` to whatever it feeds (the Search node). */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/workflow/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Text query" status={rt.status} {selected}>
		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="q-{id}">Query text</label>
		<input id="q-{id}" class="{FIELD_CLASS} w-full" placeholder="e.g. Sverige" bind:value={cfg.q} />
		<p class="text-muted-foreground mt-1 text-[0.7rem]">Drives FTS / vector legs of Search.</p>
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
