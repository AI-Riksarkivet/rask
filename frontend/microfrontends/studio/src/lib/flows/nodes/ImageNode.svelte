<script lang="ts">
	/** Image source. Emits the uploaded file's bytes downstream — wire it into a
	 *  Model node, which POSTs them to a live Serve app (htrflow reads a page
	 *  scan and answers ALTO XML). */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);

	let preview = $state<string | null>(null);

	function onFile(e: Event): void {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		if (preview) {
			URL.revokeObjectURL(preview);
			preview = null;
		}
		if (file) preview = URL.createObjectURL(file);
		graph.setConfig(id, { file, fileName: file?.name ?? '' });
	}

	// Revoke the live preview URL when this node unmounts (delete / reset), so
	// blob URLs don't leak across add → preview → delete cycles.
	$effect(() => () => {
		if (preview) URL.revokeObjectURL(preview);
	});
</script>

{#if cfg && rt}
	<NodeShell {id} title="Image" status={rt.status} {selected}>
		<input
			type="file"
			accept="image/*"
			class="nodrag text-muted-foreground file:bg-secondary file:text-secondary-foreground w-full text-[0.7rem] file:mr-2 file:rounded-md file:border-0 file:px-2 file:py-1 file:text-[0.7rem]"
			onchange={onFile}
		/>
		{#if preview}
			<img
				src={preview}
				alt={cfg.fileName || 'input image'}
				class="border-border bg-muted mt-2 h-24 w-full rounded-md border object-cover"
			/>
		{:else if cfg.fileName}
			<p class="mt-2 text-[0.7rem] text-amber-500">
				Previously: {cfg.fileName} — re-upload (images aren't saved across reloads).
			</p>
		{/if}
		<p class="text-muted-foreground mt-1 text-[0.7rem]">
			A page scan works best — wire it into a Model node running /htrflow.
		</p>
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
