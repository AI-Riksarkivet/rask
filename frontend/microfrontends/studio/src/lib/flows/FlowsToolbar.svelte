<script lang="ts">
	/** Run controls, one compact row floating over the canvas. Deliberately SMALL: it also carried
	 *  the saved-flow picker, the API snippet and five lines of help text, which made a floating card
	 *  ~550 px tall sitting on top of the graph. Those moved to the bottom drawer, where they have
	 *  width and do not overlap anything; the engine chip went to the drawer's status line too.
	 *  Adding nodes lives in the rail's node library. */
	import { LoaderCircle, Play, Redo2, RotateCcw, Undo2, Wand2 } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { graph } from '$lib/flows/graph.svelte';

	const iconBtn = 'nodrag';
</script>

<div
	class="border-border bg-card/95 flex items-center gap-1 rounded-lg border p-1 shadow-md backdrop-blur"
>
	<Button size="sm" onclick={() => graph.run()} disabled={graph.running} title="Run the whole graph">
		{#if graph.running}
			<LoaderCircle class="size-3.5 animate-spin" />
		{:else}
			<Play class="size-3.5" />
		{/if}
		Run
	</Button>
	<Button
		size="sm"
		variant="ghost"
		class={iconBtn}
		onclick={() => graph.resetRun()}
		disabled={graph.running}
		title="Clear the run state (keeps the graph)"
	>
		<RotateCcw class="size-3.5" />
	</Button>
	<span class="bg-border mx-0.5 h-4 w-px"></span>
	<Button
		size="sm"
		variant="ghost"
		class={iconBtn}
		onclick={() => graph.undo()}
		disabled={!graph.canUndo || graph.running}
		title="Undo (Ctrl/Cmd+Z)"
	>
		<Undo2 class="size-3.5" />
	</Button>
	<Button
		size="sm"
		variant="ghost"
		class={iconBtn}
		onclick={() => graph.redo()}
		disabled={!graph.canRedo || graph.running}
		title="Redo (Ctrl/Cmd+Shift+Z)"
	>
		<Redo2 class="size-3.5" />
	</Button>
	<Button
		size="sm"
		variant="ghost"
		class={iconBtn}
		onclick={() => graph.tidy()}
		disabled={graph.running}
		title="Auto-arrange left-to-right"
	>
		<Wand2 class="size-3.5" />
	</Button>
</div>
