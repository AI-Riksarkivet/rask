<script lang="ts">
	// TEXT viewer — the DOCUMENT is the canvas (the doccano/Argilla surface). No pixel medium, no
	// Pixi, no image backdrop: the unit's rows carry the text, and that text fills the central
	// workspace at reading size. Spans are inline highlights; selecting text labels in place, by
	// click or by digit hotkey; picking a line is the same selection the queue and inspector share.
	//
	// Rides the controller's TEMPORAL seam (`attachData`) exactly like audio: full data + review +
	// Save path, no engine. One registry entry, one component — the modality contract holding.
	import { loadAnnotations } from '@rask/labeling/annotations-client';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import { spanColor } from './span-render';
	import { textLaneLines } from './text-lane';
	import TextSpanSurface from './layout/TextSpanSurface.svelte';
	import type { ViewerProps } from './types';

	let { unit, onload, onerror, controller }: ViewerProps = $props();

	$effect(() => {
		if (!controller) return;
		const url = unit.annotationsUrl;
		void loadAnnotations(url)
			.then(({ table, version }) => {
				controller.attachData(table, url, version);
				onload?.(controller.rows.length);
			})
			.catch((e: unknown) => onerror?.(e instanceof Error ? e.message : String(e)));
	});

	const lines = $derived(textLaneLines(controller?.rows ?? []));
	const spanClasses = $derived(controller?.textSpanClasses ?? []);

	const marksFor = (lineId: string) =>
		(controller?.rows ?? [])
			.filter((r) => r.parentId === lineId && r.charStart != null && r.charStart >= 0)
			.map((r) => ({
				index: r.index,
				id: r.id,
				start: r.charStart ?? 0,
				end: r.charEnd ?? 0,
				label: r.label,
			}));

	function trackSelection(el: HTMLElement, index: number) {
		$effect(() => {
			if (controller?.selectedIndex === index)
				el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
		});
	}
</script>

<div class="bg-background h-full w-full overflow-y-auto" data-testid="text-canvas">
	<div class="mx-auto flex max-w-3xl flex-col gap-1 px-8 py-6">
		{#if spanClasses.length}
			<!-- The class legend IS the keymap: select text, press the digit. Pinned above the
			     document so the vocabulary is on screen the whole session, like doccano's rail. -->
			<div
				class="bg-background/95 sticky top-0 z-10 flex flex-wrap items-center gap-1.5 pb-3 backdrop-blur"
				data-testid="text-canvas-legend"
			>
				{#each spanClasses as c, i (c)}
					<span
						class="border-border bg-card inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs"
						style={`border-bottom: 2px solid ${spanColor(c, spanClasses)}`}
					>
						<kbd class="text-muted-foreground font-mono text-[10px]">{i + 1}</kbd>
						{c}
					</span>
				{/each}
				<span class="text-muted-foreground ml-auto text-[11px]">
					select text · press a digit or click a class
				</span>
			</div>
		{/if}

		{#each lines as line, n (line.id)}
			<div
				class={cn(
	'group flex items-start gap-3 rounded-md py-0.5',
	controller?.selectedIndex === line.index && 'bg-accent/40',
)}
				data-testid="text-canvas-line"
				use:trackSelection={line.index}
			>
				<Button
					variant={controller?.selectedIndex === line.index ? 'secondary' : 'ghost'}
					size="xs"
					class="text-muted-foreground mt-2 w-9 shrink-0 justify-end tabular-nums opacity-60 group-hover:opacity-100"
					title={line.label ? `Select this line (${line.label})` : 'Select this line'}
					data-testid="text-canvas-pick"
					onclick={() => controller?.select(line.index)}
				>
					{n + 1}
				</Button>
				<div class="min-w-0 flex-1">
					<TextSpanSurface
						text={line.text}
						marks={marksFor(line.id)}
						classes={spanClasses}
						selectedIndex={controller?.selectedIndex ?? null}
						hint={false}
						textClass="text-base leading-9"
						testid={`doc-surface-${n}`}
						onlabel={(start, end, label) => controller?.addTextSpan(line.index, start, end, label)}
						onpick={(i) => controller?.select(i)}
						onremove={(i) => controller?.deleteRow(i)}
					/>
				</div>
			</div>
		{:else}
			<p class="text-muted-foreground py-12 text-center text-sm">
				This unit carries no text — nothing to read, nothing to label.
			</p>
		{/each}
	</div>
</div>
