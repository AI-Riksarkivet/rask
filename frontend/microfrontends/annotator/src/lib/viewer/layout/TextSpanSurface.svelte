<script lang="ts">
	// The TEXT-AS-SURFACE span annotator (the doccano/Label-Studio NER interaction): the
	// transcription renders as readable flowing text, existing spans as class-coloured inline
	// highlights with a label tag where each ends, and selecting text IN the surface labels a new
	// span right there. Controlled like every layout component — rows in, intents out; the
	// segmentation/colour model lives in `span-render.ts` where it is testable without a DOM.
	import { X } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import { type SpanMark, segmentText, spanColor } from '../span-render';

	let {
		text,
		marks,
		classes,
		selectedIndex = null,
		testid = 'span-surface',
		hint = true,
		textClass = 'text-sm leading-8',
		onlabel,
		onpick,
		onremove,
	}: {
		text: string;
		marks: SpanMark[];
		/** The task's text classes — the label buttons a fresh selection offers. */
		classes: string[];
		selectedIndex?: number | null;
		/** Test handle — the surface mounts in the inspector AND per-line in the transcription
		 *  lane, and two anonymous copies would be unlocatable apart. */
		testid?: string;
		/** Show the idle "select text…" hint. Off in the transcription lane, where one hint per
		 *  line would repeat itself down the whole page. */
		hint?: boolean;
		/** Typography of the text body — the document canvas reads at document size, the
		 *  inspector's copy stays compact. */
		textClass?: string;
		onlabel?: (start: number, end: number, label: string) => void;
		onpick?: (index: number) => void;
		onremove?: (index: number) => void;
	} = $props();

	const segments = $derived(segmentText(text, marks));

	// The pending selection — captured from the browser's own selection on mouseup, cleared on
	// commit or an empty click. Offsets are absolute into `text`, recovered from each segment's
	// data-start plus the selection's offset within it.
	let pending = $state<{ start: number; end: number } | null>(null);

	function offsetAt(node: Node, offset: number): number | null {
		const el = node instanceof Element ? node : ((node.parentElement as Element | null) ?? null);
		const seg = el?.closest('[data-start]') as HTMLElement | null;
		if (!seg?.dataset.start) return null;
		return Number(seg.dataset.start) + offset;
	}

	let container = $state<HTMLElement | null>(null);

	/** Window-level on purpose: many surfaces mount at once (one per document line), and each
	 *  clears its pending unless the selection landed INSIDE it — so exactly one surface holds a
	 *  pending span at a time, and the digit hotkeys below can never label two lines with one key. */
	function captureSelection(): void {
		const sel = window.getSelection();
		if (
			!sel ||
			sel.isCollapsed ||
			sel.rangeCount === 0 ||
			!container ||
			!container.contains(sel.anchorNode)
		) {
			pending = null;
			return;
		}
		const a = offsetAt(sel.anchorNode!, sel.anchorOffset);
		const b = offsetAt(sel.focusNode!, sel.focusOffset);
		if (a == null || b == null || a === b) {
			pending = null;
			return;
		}
		pending = { start: Math.min(a, b), end: Math.max(a, b) };
	}

	/** The doccano keyboard: with a selection pending, digits 1–9 label it with the nth class —
	 *  the whole gesture is select · press · next. Only the surface holding the selection reacts. */
	function onHotkey(e: KeyboardEvent): void {
		if (!pending) return;
		const el = e.target as HTMLElement | null;
		const tag = el?.tagName?.toLowerCase();
		if (tag === 'input' || tag === 'textarea' || el?.isContentEditable) return;
		const n = Number(e.key);
		if (!Number.isInteger(n) || n < 1 || n > classes.length) return;
		e.preventDefault();
		commit(classes[n - 1]!);
	}

	function commit(label: string): void {
		if (!pending) return;
		onlabel?.(pending.start, pending.end, label);
		pending = null;
		window.getSelection()?.removeAllRanges();
	}

	/** Highlight tint: the class colour at surface strength; full-strength underline carries the
	 *  hue for colour-blind-safe reading, doubled (thicker) where spans overlap. */
	const tint = (label: string) =>
		`color-mix(in oklab, ${spanColor(label, classes)} 26%, transparent)`;
</script>

<svelte:window onmouseup={captureSelection} onkeydown={onHotkey} />

<div class="flex flex-col gap-1.5" data-testid={testid}>
	<div
		bind:this={container}
		class={cn(
			'border-input bg-background selection:bg-primary/25 rounded-md border p-2.5 select-text',
			textClass,
		)}
	>
		{#each segments as seg (seg.start)}
			{#if seg.marks.length > 0}
				{@const top = seg.marks[seg.marks.length - 1]!}
				<mark
					data-start={seg.start}
					class={cn(
						'cursor-pointer rounded-sm py-0.5 text-inherit',
						selectedIndex != null &&
							seg.marks.some((m) => m.index === selectedIndex) &&
							'ring-ring ring-1',
					)}
					style:background-color={tint(top.label)}
					style:box-shadow={seg.marks.length > 1
						? `inset 0 -2px ${spanColor(seg.marks[0]!.label, classes)}, inset 0 -4px ${spanColor(top.label, classes)}`
						: `inset 0 -2px ${spanColor(top.label, classes)}`}
					onclick={() => onpick?.(top.index)}>{seg.text}</mark
				>{#each seg.ending as m (m.id)}<span
						class="bg-card text-muted-foreground border-border mr-0.5 ml-0.5 inline-flex translate-y-[-0.45em] items-center gap-0.5 rounded border px-1 align-middle text-[9px] font-medium tracking-wide uppercase"
						style:border-bottom={`2px solid ${spanColor(m.label, classes)}`}
						data-testid="span-tag">{m.label}<button
							class="hover:text-destructive -mr-0.5 inline-flex"
							title="Remove this span"
							aria-label={`Remove ${m.label} span`}
							onclick={(e) => {
								e.stopPropagation();
								onremove?.(m.index);
							}}
						><X class="size-2.5" /></button></span>{/each}
			{:else}<span data-start={seg.start}>{seg.text}</span>{/if}
		{/each}
	</div>

	{#if pending}
		<!-- The labeler follows the selection — doccano's gesture: select, then name it. One button
		     per declared class, so labeling is one click and never a dropdown round-trip. -->
		<div class="flex flex-wrap items-center gap-1.5" data-testid="span-labeler">
			<span class="text-muted-foreground max-w-48 truncate font-mono text-xs">
				“{text.slice(pending.start, pending.end)}”
			</span>
			{#each classes as c (c)}
				<Button
					size="xs"
					variant="outline"
					style={`border-bottom: 2px solid ${spanColor(c, classes)}`}
					data-testid={`label-as-${c}`}
					onclick={() => commit(c)}
				>
					{c}
				</Button>
			{/each}
		</div>
	{:else if hint}
		<span class="text-muted-foreground text-[11px]">
			Select text above to label a span{classes.length
				? ''
				: ' — this task declares no text classes'}.
		</span>
	{/if}
</div>
