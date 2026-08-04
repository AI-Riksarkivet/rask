<script lang="ts">
	/** Image input. Emits `{ image }` (a File) to the Search node, which POSTs it
	 *  multipart so the backend embeds it for the visual leg. Set the Search mode
	 *  to Image (or All) to actually rank by frame similarity. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/workflow/graph.svelte';
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
		graph.setConfig(id, { image: file, imageName: file?.name ?? '' });
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
				alt={cfg.imageName || 'query image'}
				class="border-border bg-muted mt-2 h-24 w-full rounded-md border object-cover"
			/>
		{:else if cfg.imageName}
			<p class="mt-2 text-[0.7rem] text-amber-500">
				Previously: {cfg.imageName} — re-upload (images aren't saved across reloads).
			</p>
		{/if}
		<p class="text-muted-foreground mt-1 text-[0.7rem]">
			Wire into a Search's <span class="text-violet-400">img</span> port, then set mode =
			<span class="text-foreground">Image</span> or <span class="text-foreground">All</span>.
		</p>
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
