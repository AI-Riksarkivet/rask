<script lang="ts">
	// The TRANSCRIPTION LANE — the document's text as a first-class surface of the central
	// workspace, under the page image (the Label-Studio OCR/transcription composition). Every row
	// carrying text renders as a line in reading order; each line is a full span-labeling surface,
	// and picking a line is the same selection the canvas and inspector share — click the line
	// number here, watch the box highlight on the image above.
	//
	// This is where text annotation LIVES now; the inspector keeps a per-row copy for the selected
	// row, but reading and labeling a page's text no longer means working inside a side panel.
	import { ChevronDown, ChevronUp } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import type { AnnotatorController } from '../annotator.svelte';
	import { textLaneLines } from '../text-lane';
	import TextSpanSurface from './TextSpanSurface.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	const lines = $derived(textLaneLines(controller.rows));
	const spanClasses = $derived(controller.textSpanClasses);
	let collapsed = $state(false);

	const marksFor = (lineId: string) =>
		controller.rows
			.filter((r) => r.parentId === lineId && r.charStart != null && r.charStart >= 0)
			.map((r) => ({
				index: r.index,
				id: r.id,
				start: r.charStart ?? 0,
				end: r.charEnd ?? 0,
				label: r.label,
			}));

	/** Scroll the selected line into view — the canvas picked it, the lane follows. */
	function trackSelection(el: HTMLElement, index: number) {
		$effect(() => {
			if (controller.selectedIndex === index)
				el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
		});
	}
</script>

{#if lines.length > 0}
	<div
		class="border-border bg-card flex max-h-[40%] shrink-0 flex-col border-t"
		data-testid="text-lane"
	>
		<div class="flex items-center justify-between px-3 py-1">
			<span class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
				Transcription · {lines.length} line{lines.length === 1 ? '' : 's'}
			</span>
			<Button
				variant="ghost"
				size="icon-xs"
				title={collapsed ? 'Show the transcription' : 'Collapse the transcription'}
				aria-expanded={!collapsed}
				data-testid="text-lane-toggle"
				onclick={() => (collapsed = !collapsed)}
			>
				{#if collapsed}<ChevronUp class="size-3.5" />{:else}<ChevronDown class="size-3.5" />{/if}
			</Button>
		</div>
		{#if !collapsed}
			<div class="flex min-h-0 flex-col gap-1 overflow-y-auto px-3 pb-2">
				{#each lines as line, n (line.id)}
					<div
						class={cn(
							'flex items-start gap-2 rounded-md p-1',
							controller.selectedIndex === line.index && 'bg-accent/50',
						)}
						data-testid="text-lane-line"
						use:trackSelection={line.index}
					>
						<!-- The line's HANDLE: picks the row everywhere (canvas box, inspector, here).
						     A separate element, so text selection inside the surface never doubles as
						     a row click. -->
						<Button
							variant={controller.selectedIndex === line.index ? 'secondary' : 'ghost'}
							size="xs"
							class="mt-1 w-8 shrink-0 justify-center tabular-nums"
							title={line.label ? `Select this line (${line.label})` : 'Select this line'}
							data-testid="text-lane-pick"
							onclick={() => controller.select(line.index)}
						>
							{n + 1}
						</Button>
						<div class="min-w-0 flex-1">
							<TextSpanSurface
								text={line.text}
								marks={marksFor(line.id)}
								classes={spanClasses}
								selectedIndex={controller.selectedIndex}
								hint={false}
								testid={`lane-surface-${n}`}
								onlabel={(start, end, label) =>
									controller.addTextSpan(line.index, start, end, label)}
								onpick={(i) => controller.select(i)}
								onremove={(i) => controller.deleteRow(i)}
							/>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}
