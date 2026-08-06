<script lang="ts">
	/** Prompt — compose the text a Model node will send, from a template plus the upstream payload.
	 *  Real, not scaffold: it is a pure string transform, and it is the piece that turns a
	 *  transcription into something an LLM deployment can be asked about. `{{input}}` is substituted
	 *  verbatim (prose, not a wire body — the JSON escaping lives in the Model node's body mode). */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Prompt" status={rt.status} {selected}>
		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="prompt-{id}">Template</label>
		<textarea
			id="prompt-{id}"
			class="{FIELD_CLASS} nowheel h-24 w-full resize-none"
			placeholder="Ask something about {'{{input}}'}…"
			bind:value={cfg.promptTemplate}></textarea>
		<p class="text-muted-foreground mt-1 text-[0.65rem]">
			<code>{'{{input}}'}</code> becomes the upstream text.
		</p>
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
