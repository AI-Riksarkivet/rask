<script lang="ts">
	/** Text source. Emits its text downstream — the seed for text-taking
	 *  endpoints, or straight into Inspect to sanity-check the wiring. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Text" status={rt.status} {selected}>
		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="text-{id}">Text</label>
		<textarea
			id="text-{id}"
			class="{FIELD_CLASS} h-20 w-full resize-none"
			placeholder="Type the payload to send downstream…"
			bind:value={cfg.text}></textarea>
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
