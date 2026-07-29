<script module lang="ts">
	/** MIME carrying the node kind across the HTML5 drag (palette → canvas drop). */
	export const NODE_DND_MIME = 'application/lance-media-node';
</script>

<script lang="ts">
	/** The single way to add nodes: drag an item onto the canvas to drop a new
	 *  node where you release (the FlowPane handles ondrop). Replaces the old
	 *  toolbar "Add" row. */
	import { GripVertical } from '@lucide/svelte';
	import { NODE_KINDS, nodeLabel } from '$lib/workflow/graph.svelte';

	function onDragStart(e: DragEvent, kind: string): void {
		if (!e.dataTransfer) return;
		e.dataTransfer.setData(NODE_DND_MIME, kind);
		e.dataTransfer.effectAllowed = 'move';
	}
</script>

<div
	class="border-border bg-card/95 flex max-w-[8.5rem] flex-col gap-1 rounded-lg border p-1.5 shadow-md backdrop-blur"
>
	<span class="text-muted-foreground px-1 text-[0.7rem] font-medium tracking-wide uppercase">
		Drag to add
	</span>
	{#each NODE_KINDS as kind (kind)}
		<button
			type="button"
			draggable="true"
			ondragstart={(e) => onDragStart(e, kind)}
			title="Drag onto the canvas to add a {nodeLabel(kind)} node"
			class="border-border bg-background text-foreground hover:bg-muted flex cursor-grab items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors active:cursor-grabbing"
		>
			<GripVertical class="text-muted-foreground size-3 shrink-0" />
			<span class="truncate">{nodeLabel(kind)}</span>
		</button>
	{/each}
</div>
