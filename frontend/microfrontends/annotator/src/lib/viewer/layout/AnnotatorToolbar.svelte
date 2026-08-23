<script lang="ts">
	import type { Snippet } from 'svelte';
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
		SlidersHorizontal,
	} from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import * as Popover from '@rask/ui/popover';
	import { Slider } from '@rask/ui/slider';
	import { cn } from '@rask/ui/utils';
	import { COMMITS_SHAPE } from '@rask/labeling/shape-types';
	import { TOOL_DEFS, bandsOf } from '../tool-defs';
	import type { AnnotatorController } from '../annotator.svelte';

	// `spatial` = this unit has a canvas to draw ON (image / video frame). Audio has no
	// spatial lane — its segments are made by dragging on the waveform — so the draw
	// tools + pan + convert-to-polygon are hidden; mode/undo/redo/save/delete stay.
	// `onexit` (optional) renders the back-to-selection button at the rail top.
	let {
		assist,
		controller,
		spatial = true,
		onexit,
	}: {
		controller: AnnotatorController;
		spatial?: boolean;
		onexit?: () => void;
		/** The AI-assist segment, rendered INSIDE this bar rather than floating over the canvas.
		 *
		 *  A snippet rather than a component import, so the toolbar stays ignorant of what assist IS:
		 *  it owns placement, the shell owns what goes there. That keeps the "AI is part of the
		 *  toolbar" ruling from turning into a dependency from chrome onto the assist plane. */
		assist?: Snippet;
	} = $props();

	// A DRAWING tool is offered only when the open task's ontology permits the shape it commits.
	// `ProjectsLanding.svelte` claimed this already happened; it did not, so an annotator drew with
	// a tool the task would refuse and only learned at submit — enforcement that punishes rather
	// than teaches. `allowedTools` is null when unconstrained, so an ad-hoc canvas is unchanged.
	//
	// Only drawing tools are filtered: select, pan and lasso navigate rather than commit, so no task
	// restriction should ever hide them.
	const visible = $derived(
		spatial
			? TOOL_DEFS.filter(
					(t) =>
						(!t.drawing || controller.canDraw) &&
						(!t.cv || controller.cvCapable) &&
						(!COMMITS_SHAPE.has(t.tool) ||
							controller.allowedTools === null ||
							controller.allowedTools.has(t.tool)),
				)
			: [],
	);

	/** The rail in BANDS: navigate · draw · assist, in reading order.
	 *
	 *  Ten buttons in one undifferentiated column asked the annotator to remember which of them
	 *  commits a shape and which only moves the view. The bands say it.
	 *
	 *  EMPTY BANDS ARE DROPPED, and that is the load-bearing part: the filter above already hides a
	 *  drawing tool the task refuses, so a bbox-only task empties `assist` entirely — and a separator
	 *  rendered for an absent band is a hairline with nothing on either side of it. Deriving the
	 *  bands from what SURVIVED the filter is what keeps the two in agreement. */
	const bands = $derived(bandsOf(visible));
</script>

<!-- HORIZONTAL, above the canvas — reported: "the sidebar toolbar horizonally over the canvas.
     and why is not the ai stuff a part of the toolbar?"
     Both halves of that are the same complaint. A vertical rail on the left competed with the
     filmstrip for the edge the ITEMS belong on, and it pushed the AI actions out into a floating
     bar of their own — which said that assisting is a different kind of act from drawing. It is
     not: both produce annotations, and both belong on the one surface that produces annotations.
     (The float also collided: `AiAssistBar` and `TaskStreamNav` were both `absolute top-2 left-1/2`.)

     It keeps the estate's SIDEBAR tokens rather than card — it is still this zone's chrome, now a
     top bar rather than a left one, and it should read as the same surface the other rails occupy.
     `overflow-x-auto` because a wide ontology can outrun a narrow canvas, and a toolbar that
     WRAPS moves the canvas down a row as tools appear. -->
<div
	class="border-sidebar-border bg-sidebar flex h-11 w-full shrink-0 items-center gap-1 overflow-x-auto border-b px-2"
	data-testid="annotator-toolbar"
>
	{#if onexit}
		<Button
			variant="ghost"
			size="icon-sm"
			title="Leave the canvas — back to the queue you came from, or the corpus browser"
			data-testid="exit-annotate"
			onclick={onexit}
		>
			<LayoutGrid class="size-4" />
		</Button>
		<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>
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
		<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>

		{#each bands as band, i (band.group)}
			{#if i > 0}
				<!-- Between bands only — never before the first, never after the last. -->
				<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0" data-testid="tool-band-sep"></div>
			{/if}
			{#each band.tools as t (t.tool)}
				{@const Icon = t.icon}
				{@const cvLoading =
					t.cv === true && controller.activeTool === t.tool && !controller.cvReady.has(t.tool)}
				<Button
					variant={controller.activeTool === t.tool ? 'default' : 'ghost'}
					size="icon-sm"
					title={`${t.label} (${t.key})${cvLoading ? ' — detecting corners…' : ''}`}
					aria-pressed={controller.activeTool === t.tool}
					disabled={!controller.canDraw && COMMITS_SHAPE.has(t.tool)}
					data-cvready={t.cv ? controller.cvReady.has(t.tool) : undefined}
					data-snapped={t.tool === 'magnetic' ? controller.magneticSnapped : undefined}
					onclick={() => controller.setTool(t.tool)}
				>
					<Icon class={cn('size-4', cvLoading && 'animate-pulse')} />
				</Button>
			{/each}
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
			<!-- The brush's other two knobs. These were engine-settable from day one and reachable from
			     nowhere — the eraser above was the ONLY exposed control, so every stroke painted at the
			     default 20 px and always committed a raster mask. -->
			<div
				class="flex shrink-0 items-center gap-1.5 px-1"
				data-testid="brush-controls"
				title="Brush size"
			>
				<Slider
					class="w-24"
					min={4}
					max={80}
					step={2}
					value={controller.brushOptions.radius}
					aria-label="Brush size"
					onValueChange={(radius) => controller.setBrushOptions({ radius })}
				/>
				<span class="text-muted-foreground w-6 text-right text-[10px] tabular-nums">
					{controller.brushOptions.radius}
				</span>
			</div>
			<Button
				variant="ghost"
				size="sm"
				class="shrink-0 px-2 text-[10px] font-medium uppercase tracking-wide"
				title={controller.brushOptions.output === 'mask'
					? 'Commits a raster mask (click to commit a traced polygon instead)'
					: 'Commits a traced polygon (click to commit a raster mask instead)'}
				data-testid="brush-output"
				onclick={() =>
					controller.setBrushOptions({
						output: controller.brushOptions.output === 'mask' ? 'polygon' : 'mask',
					})}
			>
				{controller.brushOptions.output === 'mask' ? 'Mask' : 'Poly'}
			</Button>
		{/if}
	{/if}

	{#if spatial}
		<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>
		<!-- Display-only adjustments for the PAGE IMAGE (faded ink needs a contrast lift to trace).
		     The engine seam (`ImagePlugin.setImageAdjustments`) existed with no caller — this popover
		     is its first. Never persisted: it themes the view, not the data. -->
		<Popover.Root>
			<Popover.Trigger>
				{#snippet child({ props })}
					<Button
						{...props}
						variant={controller.imageAdjusted ? 'default' : 'ghost'}
						size="icon-sm"
						title={controller.imageAdjusted
							? 'Image adjustments (active — not saved with annotations)'
							: 'Image adjustments (brightness · contrast · saturation)'}
						data-testid="image-adjust-trigger"
					>
						<SlidersHorizontal class="size-4" />
					</Button>
				{/snippet}
			</Popover.Trigger>
			<Popover.Content class="w-64 p-3" data-testid="image-adjust">
				<div class="flex flex-col gap-3">
					{#each [['brightness', 'Brightness'], ['contrast', 'Contrast'], ['saturation', 'Saturation']] as const as [key, label] (key)}
						<div class="flex items-center gap-2">
							<span class="text-muted-foreground w-20 shrink-0 text-xs">{label}</span>
							<Slider
								class="flex-1"
								min={0.2}
								max={2}
								step={0.05}
								value={controller.imageAdjust[key]}
								aria-label={label}
								onValueChange={(value) => controller.setImageAdjust({ [key]: value })}
							/>
							<span class="text-muted-foreground w-8 shrink-0 text-right text-[10px] tabular-nums">
								{controller.imageAdjust[key].toFixed(2)}
							</span>
						</div>
					{/each}
					<Button
						variant="ghost"
						size="sm"
						class="self-end"
						disabled={!controller.imageAdjusted}
						data-testid="image-adjust-reset"
						onclick={() => controller.resetImageAdjust()}
					>
						Reset
					</Button>
				</div>
			</Popover.Content>
		</Popover.Root>
	{/if}

	{#if assist}
		<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>
		{@render assist()}
	{/if}

	{#if spatial && !controller.attached}
		<!-- Say it WHERE THE TOOLS ARE. The status chip sits bottom-left and reads "load failed",
		     which a person parses as "the picture didn't load" — not as "everything you draw here is
		     discarded". This sits in the bar you just tried to use.

		     `spatial &&` is not decoration. A TEMPORAL unit (audio) never attaches a PixiContext at
		     all — the controller's own contract says spatial viewers attach one and a temporal viewer
		     brings its own surface — so `attached` is legitimately false there. Without this guard the
		     first cut of this notice cried "read-only — annotations unavailable" over a perfectly
		     healthy waveform that had just reported "1 annotations from Lance". Caught by driving the
		     AUDIO modality, which nothing else in this session had exercised. -->
		<span
			class="text-destructive ml-2 shrink-0 text-xs font-medium"
			data-testid="canvas-readonly"
			title="The annotations for this item could not be read, so editing is disabled — saving would overwrite work that was never loaded."
		>
			read-only — annotations unavailable
		</span>
	{/if}

	<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>

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

	<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>

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
