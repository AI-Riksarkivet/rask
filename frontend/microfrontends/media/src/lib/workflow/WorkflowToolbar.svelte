<script lang="ts">
	/** Canvas run controls (Run / Clear / Reset / Delete / Undo / Redo / Tidy).
	 *  Adding nodes lives in the drag-to-add palette, not here. */
	import {
		Play,
		RotateCcw,
		LoaderCircle,
		Trash2,
		Undo2,
		Redo2,
		Wand2,
		Command,
	} from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { graph } from '$lib/workflow/graph.svelte';
	import { commandMenu } from '$lib/workflow/command-menu.svelte';
</script>

<div
	class="border-border bg-card/95 flex flex-col gap-2 rounded-lg border p-2 shadow-md backdrop-blur"
>
	<div class="flex items-center gap-1.5">
		<Button size="sm" onclick={() => graph.run()} disabled={graph.running}>
			{#if graph.running}
				<LoaderCircle class="size-3.5 animate-spin" />
			{:else}
				<Play class="size-3.5" />
			{/if}
			Run
		</Button>
		<Button size="sm" variant="outline" onclick={() => graph.resetRun()} disabled={graph.running}>
			Clear run
		</Button>
		<Button size="sm" variant="ghost" onclick={() => graph.reset()} disabled={graph.running}>
			<RotateCcw class="size-3.5" />
			Reset
		</Button>
		<Button
			size="sm"
			variant="outline"
			onclick={() => graph.deleteSelected()}
			disabled={!graph.hasSelection || graph.running}
			title="Delete the selected node(s) / edge(s)"
		>
			<Trash2 class="size-3.5" />
			Delete
		</Button>
	</div>
	<div class="flex items-center gap-1.5">
		<Button
			size="sm"
			variant="outline"
			onclick={() => graph.undo()}
			disabled={!graph.canUndo || graph.running}
			title="Undo (Ctrl/Cmd+Z)"
		>
			<Undo2 class="size-3.5" />
			Undo
		</Button>
		<Button
			size="sm"
			variant="outline"
			onclick={() => graph.redo()}
			disabled={!graph.canRedo || graph.running}
			title="Redo (Ctrl/Cmd+Shift+Z)"
		>
			<Redo2 class="size-3.5" />
			Redo
		</Button>
		<Button
			size="sm"
			variant="ghost"
			onclick={() => graph.tidy()}
			disabled={graph.running}
			title="Auto-layout the graph left-to-right"
		>
			<Wand2 class="size-3.5" />
			Tidy
		</Button>
		<Button
			size="sm"
			variant="ghost"
			onclick={() => commandMenu.toggle()}
			title="All commands & shortcuts (Ctrl/⌘ K)"
		>
			<Command class="size-3.5" />
			⌘K
		</Button>
	</div>
	{#if graph.lastError}
		<div class="text-destructive max-w-[18rem] text-[0.7rem]">{graph.lastError}</div>
	{/if}
	<div class="text-muted-foreground/80 max-w-[19rem] space-y-0.5 text-[0.7rem]">
		<div>
			<span class="text-foreground">Add</span> — drag a node from the palette (top-right) onto the canvas.
		</div>
		<div>
			<span class="text-foreground">Connect</span> — drag a node's right ● onto another's left ●. A Search
			accepts several inputs at once (a query/image + a refine).
		</div>
		<div>
			<span class="text-foreground">Run one node</span> — hover a node and press ▶: upstream results are
			reused, missing upstream runs once. Shift+▶ reruns the whole branch; an amber “stale” chip means upstream
			changed since that node last ran.
		</div>
		<div>
			<span class="text-foreground">Delete</span> — hover a node and click ✕, or select a node/edge and press
			⌫ (or the Delete button).
		</div>
		<div>
			<span class="text-foreground">Refine</span> — Search → Search scopes the second to the first's videos.
			Click any node to inspect it on the right.
		</div>
		<div>
			<span class="text-foreground">Export</span> — wire results into an Export node, then pick the format
			&amp; columns in the inspector and download.
		</div>
	</div>
</div>
