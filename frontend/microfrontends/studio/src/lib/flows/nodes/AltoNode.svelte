<script lang="ts">
	/** ALTO → text transform: turns the Model node's ALTO XML into plain text,
	 *  one line per <TextLine>. Pure client-side — the composability proof that
	 *  a flow is more than one endpoint call. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);

	const lineCount = $derived.by(() => {
		const p = rt?.output?.payload;
		if (!p || p.kind !== 'text' || !p.text) return null;
		return p.text.split('\n').length;
	});
</script>

{#if cfg && rt}
	<NodeShell {id} title="ALTO → text" status={rt.status} {selected}>
		<p class="text-muted-foreground text-[0.7rem]">
			Extracts the transcription out of ALTO XML — wire it after a Model node.
		</p>
		{#if lineCount !== null}
			<p class="mt-2 text-[0.7rem]">
				<span class="text-foreground font-medium">{lineCount}</span>
				<span class="text-muted-foreground">text line{lineCount === 1 ? '' : 's'}</span>
			</p>
		{/if}
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
