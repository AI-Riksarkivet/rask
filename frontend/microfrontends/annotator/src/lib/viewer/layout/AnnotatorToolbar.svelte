<script lang="ts">
	// Left tool rail — the primary command surface. Fully controlled: reads/writes
	// the AnnotatorController facade, never the engine directly. (Ported from
	// ra-anno Toolbar.svelte, trimmed to functional controls for our engine.)
	import {
		Eye,
		LayoutGrid,
		Pencil,
		Trash2,
		Spline,
		Eraser,
		Undo2,
		Redo2,
		Save,
	} from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import { TOOL_DEFS } from '../tool-defs';
	import type { AnnotatorController } from '../annotator.svelte';

	// `spatial` = this unit has a canvas to draw ON (image / video frame). Audio has no
	// spatial lane — its segments are made by dragging on the waveform — so the draw
	// tools + pan + convert-to-polygon are hidden; mode/undo/redo/save/delete stay.
	// `onexit` (optional) renders the back-to-selection button at the rail top.
	let {
		controller,
		spatial = true,
		onexit,
	}: { controller: AnnotatorController; spatial?: boolean; onexit?: () => void } = $props();

	const visible = $derived(
		spatial
			? TOOL_DEFS.filter(
					(t) => (!t.drawing || controller.canDraw) && (!t.cv || controller.cvCapable),
				)
			: [],
	);
</script>

<!-- The rail wears the estate's SIDEBAR tokens, not card: it is this zone's left navigation
     rail, so it should read as the same surface the data/admin sidebars occupy. -->
<div
	class="border-sidebar-border bg-sidebar flex h-full w-11 shrink-0 flex-col items-center gap-1 border-r py-2"
	data-testid="annotator-toolbar"
>
	{#if onexit}
		<Button
			variant="ghost"
			size="icon-sm"
			title="Back to document selection"
			data-testid="exit-annotate"
			onclick={onexit}
		>
			<LayoutGrid class="size-4" />
		</Button>
		<div class="bg-sidebar-border my-1 h-px w-6"></div>
	{/if}

	<!-- Mode toggle -->
	<Button
		variant={controller.mode === 'edit' ? 'default' : 'ghost'}
		size="icon-sm"
		title={controller.mode === 'edit' ? 'Edit mode (click to view)' : 'View mode (click to edit)'}
		aria-pressed={controller.mode === 'edit'}
		onclick={() => controller.toggleMode()}
	>
		{#if controller.mode === 'edit'}<Pencil class="size-4" />{:else}<Eye class="size-4" />{/if}
	</Button>

	{#if spatial}
		<div class="bg-sidebar-border my-1 h-px w-6"></div>

		{#each visible as t (t.tool)}
			{@const Icon = t.icon}
			{@const cvLoading =
				t.cv === true && controller.activeTool === t.tool && !controller.cvReady.has(t.tool)}
			<Button
				variant={controller.activeTool === t.tool ? 'default' : 'ghost'}
				size="icon-sm"
				title={`${t.label} (${t.key})${cvLoading ? ' — detecting corners…' : ''}`}
				aria-pressed={controller.activeTool === t.tool}
				data-cvready={t.cv ? controller.cvReady.has(t.tool) : undefined}
				data-snapped={t.tool === 'magnetic' ? controller.magneticSnapped : undefined}
				onclick={() => controller.setTool(t.tool)}
			>
				<Icon class={cn('size-4', cvLoading && 'animate-pulse')} />
			</Button>
		{/each}

		{#if controller.activeTool === 'brush'}
			<Button
				variant={controller.brushOptions.erasing ? 'default' : 'ghost'}
				size="icon-sm"
				title="Erase (brush)"
				aria-pressed={controller.brushOptions.erasing}
				onclick={() => controller.setBrushOptions({ erasing: !controller.brushOptions.erasing })}
			>
				<Eraser class="size-4" />
			</Button>
		{/if}
	{/if}

	<div class="bg-sidebar-border my-1 h-px w-6"></div>

	<!-- Undo / redo (field edits: relabel / accept / reject / text) -->
	<Button
		variant="ghost"
		size="icon-sm"
		title="Undo (Ctrl+Z)"
		disabled={!controller.canUndo}
		onclick={() => controller.undo()}
	>
		<Undo2 class="size-4" />
	</Button>
	<Button
		variant="ghost"
		size="icon-sm"
		title="Redo (Ctrl+Shift+Z)"
		disabled={!controller.canRedo}
		onclick={() => controller.redo()}
	>
		<Redo2 class="size-4" />
	</Button>
	<Button
		variant={controller.canSave ? 'default' : 'ghost'}
		size="icon-sm"
		title={controller.saveError ??
	(controller.dirty ? 'Save to Lance (Ctrl+S)' : 'No unsaved edits')}
		disabled={!controller.canSave}
		onclick={() => controller.save()}
	>
		<Save
			class={cn(
	'size-4',
	controller.saving && 'animate-pulse',
	controller.saveError && 'text-destructive',
)}
		/>
	</Button>

	<div class="bg-sidebar-border my-1 h-px w-6"></div>

	<!-- Selection actions -->
	{#if spatial}
		<Button
			variant="ghost"
			size="icon-sm"
			title="Convert to polygon (P)"
			disabled={controller.selectedIndex == null}
			onclick={() => controller.convertToPolygon()}
		>
			<Spline class="size-4" />
		</Button>
	{/if}
	<Button
		variant="ghost"
		size="icon-sm"
		title="Delete selected (Del)"
		disabled={controller.selectedIndex == null}
		onclick={() => controller.deleteSelected()}
	>
		<Trash2 class="size-4" />
	</Button>

	<div class="mt-auto flex flex-col items-center gap-1">
		<span
			class={cn('size-2 rounded-full', controller.dirty ? 'bg-warning' : 'bg-transparent')}
			title={controller.dirty ? 'Unsaved edits' : 'No pending edits'}
		></span>
		<span class="text-muted-foreground text-[10px] tabular-nums" title="Annotation count">
			{controller.count}
		</span>
	</div>
</div>
