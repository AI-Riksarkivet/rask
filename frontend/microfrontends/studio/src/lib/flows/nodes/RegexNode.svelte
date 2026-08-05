<script lang="ts">
	/** Regex — extract matches from the upstream text, or replace them. The cheap, universal cleanup
	 *  step every low-code pipeline has: strip a preamble a model insists on, pull the dates out of a
	 *  transcription, normalise whitespace before a comparison. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Regex" status={rt.status} {selected}>
		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="mode-{id}">Mode</label>
		<select id="mode-{id}" class="{FIELD_CLASS} w-full" bind:value={cfg.regexMode}>
			<option value="extract">Extract matches</option>
			<option value="replace">Replace matches</option>
		</select>
		<label class="text-muted-foreground mt-2 mb-1 block text-[0.7rem]" for="pat-{id}">Pattern</label>
		<input
			id="pat-{id}"
			class="{FIELD_CLASS} w-full font-mono"
			placeholder="\d{'{4}'}"
			bind:value={cfg.regexPattern}
		/>
		{#if cfg.regexMode === 'replace'}
			<label class="text-muted-foreground mt-2 mb-1 block text-[0.7rem]" for="rep-{id}">
				Replacement
			</label>
			<input
				id="rep-{id}"
				class="{FIELD_CLASS} w-full font-mono"
				placeholder="(empty removes it)"
				bind:value={cfg.regexReplace}
			/>
		{:else}
			<p class="text-muted-foreground mt-1 text-[0.65rem]">
				Capture group 1 wins when the pattern has one; always global.
			</p>
		{/if}
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
