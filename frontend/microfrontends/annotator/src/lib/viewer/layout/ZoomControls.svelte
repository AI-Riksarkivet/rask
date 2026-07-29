<script lang="ts">
	// Floating zoom cluster (bottom-right over the canvas). Controlled by the facade.
	import { ZoomIn, ZoomOut, Maximize } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	const pct = $derived(Math.round(controller.zoomPercent * 100));
</script>

<!-- Same floating-card treatment as PageNav and the assist bar (card surface, rounded-lg,
     shadow-sm) so the canvas overlays read as one family. -->
<div
	class="border-border bg-card/90 text-card-foreground pointer-events-auto absolute right-2 bottom-2 z-10 flex items-center gap-0.5 rounded-lg border p-1 shadow-sm backdrop-blur"
	data-testid="zoom-controls"
>
	<Button variant="ghost" size="icon-xs" title="Zoom out" onclick={() => controller.zoomOut()}>
		<ZoomOut class="size-4" />
	</Button>
	<Button
		variant="ghost"
		size="xs"
		class="min-w-11 tabular-nums"
		title="Reset to fit"
		onclick={() => controller.resetView()}
	>
		{pct}%
	</Button>
	<Button variant="ghost" size="icon-xs" title="Zoom in" onclick={() => controller.zoomIn()}>
		<ZoomIn class="size-4" />
	</Button>
	<div class="bg-border mx-0.5 h-4 w-px"></div>
	<Button variant="ghost" size="icon-xs" title="Fit to view" onclick={() => controller.resetView()}>
		<Maximize class="size-4" />
	</Button>
</div>
