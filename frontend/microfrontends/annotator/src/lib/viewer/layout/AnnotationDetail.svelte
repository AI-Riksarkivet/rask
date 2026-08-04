<script lang="ts">
	// Single-annotation inspector/editor. Controlled: edits route through the facade
	// (canvas + overlay updated together). (Ported from ra-anno AnnotationSidebar detail.)
	import { Check, X, RotateCcw, ChevronUp, ChevronDown, Link2, Link2Off } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Select } from '@rask/ui/select';
	import TextInput from '$lib/ui/TextInput.svelte';
	import { statusVariant } from './statusStyle';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	const row = $derived(controller.selected);
	/** The links touching THIS shape — the only place a relation is visible per-annotation. */
	const links = $derived(row ? controller.linksFor(row.id) : []);
	/** Which end this shape sits at, so "answers → v1" reads the right way round. */
	const endLabel = (l: { from_shape: string; to_shape: string }, id: string) =>
		l.from_shape === id ? `→ ${l.to_shape}` : `← ${l.from_shape}`;

	// TEXT SPANS. A span is a range INTO this annotation's text — token-classification, and the value
	// half of KIE on transcribed text. Selecting in the field the text is already in is the whole
	// interaction: no second surface, no separate mode, and the offsets come from the browser's own
	// selection rather than being counted by hand.
	let spanStart = $state(-1);
	let spanEnd = $state(-1);
	const hasSelection = $derived(spanStart >= 0 && spanEnd > spanStart);
	/** The classes a span may be labelled with — the same taxonomy everything else is judged against. */
	const spanClasses = $derived(controller.textSpanClasses);
	let spanLabel = $state('');

	/** Read the browser's selection off the text input. Called on select/keyup/mouseup rather than
	 *  watched from an effect: a selection is an EVENT, and polling for it would fight the caret. */
	function captureSelection(e: Event): void {
		const el = e.currentTarget as HTMLInputElement | HTMLTextAreaElement;
		spanStart = el.selectionStart ?? -1;
		spanEnd = el.selectionEnd ?? -1;
	}

	function commitSpan(): void {
		if (!row || !hasSelection) return;
		controller.addTextSpan(row.index, spanStart, spanEnd, spanLabel || spanClasses[0] || 'span');
		spanStart = -1;
		spanEnd = -1;
	}
</script>

{#if row}
	<div class="flex flex-col gap-3 p-3" data-testid="annotation-detail">
		<div class="flex items-center justify-between gap-2">
			<span class="text-muted-foreground text-xs font-medium">Annotation #{row.index}</span>
			<Badge variant={statusVariant(row.status)}>{row.status || '—'}</Badge>
		</div>

		<div class="text-muted-foreground flex items-center justify-between text-xs">
			<span class="tabular-nums">{controller.queuePos.at} / {controller.queuePos.of} in queue</span>
			<div class="flex gap-0.5">
				<Button
					variant="ghost"
					size="icon-xs"
					title="Previous (K / ↑)"
					onclick={() => controller.prev()}
				>
					<ChevronUp class="size-3.5" />
				</Button>
				<Button
					variant="ghost"
					size="icon-xs"
					title="Next (J / ↓)"
					onclick={() => controller.next()}
				>
					<ChevronDown class="size-3.5" />
				</Button>
			</div>
		</div>

		<label class="flex flex-col gap-1.5 text-xs">
			<span class="text-muted-foreground">Text</span>
			<TextInput
				value={row.text}
				placeholder="—"
				oninput={(e) => controller.updateField(row.index, 'text', e.currentTarget.value)}
				onselect={captureSelection}
				onkeyup={captureSelection}
				onmouseup={captureSelection}
			/>
		</label>

		<!-- Offered only when the TASK declares a class that can be drawn as text. A "label selection"
		     button on a task with no text class is a control that can produce nothing. -->
		{#if controller.allowsTextSpans && row.text}
			<div class="flex flex-wrap items-center gap-2" data-testid="span-editor">
				{#if hasSelection}
					<span class="text-muted-foreground font-mono text-xs" data-testid="span-preview">
						“{row.text.slice(spanStart, spanEnd)}” [{spanStart}, {spanEnd})
					</span>
					{#if spanClasses.length > 1}
						<Select
							bind:value={spanLabel}
							ariaLabel="Span class"
							options={spanClasses.map((c) => ({ value: c, label: c }))}
						/>
					{/if}
					<Button size="xs" data-testid="label-selection" onclick={commitSpan}>Label selection</Button>
				{:else}
					<!-- Says what to do rather than showing a dead button. -->
					<span class="text-muted-foreground text-xs">Select text above to label a span.</span>
				{/if}
			</div>
		{/if}

		<label class="flex flex-col gap-1.5 text-xs">
			<span class="text-muted-foreground">Label</span>
			<TextInput
				value={row.label}
				placeholder="—"
				oninput={(e) => controller.updateField(row.index, 'label', e.currentTarget.value)}
			/>
		</label>

		{#if controller.labelClasses.length}
			<!-- Quick-label chips ARE buttons, so they get the estate's button primitive (xs outline,
			     secondary while it is the row's current label) rather than a hand-rolled span. -->
			<div class="flex flex-wrap gap-1" title="Quick label (applies to the selection)">
				{#each controller.labelClasses as lc (lc)}
					<Button
						variant={row.label === lc ? 'secondary' : 'outline'}
						size="xs"
						aria-pressed={row.label === lc}
						onclick={() => controller.applyLabel(lc)}
					>
						{lc}
					</Button>
				{/each}
			</div>
		{/if}

		<label class="flex flex-col gap-1.5 text-xs">
			<span class="text-muted-foreground">Group</span>
			<TextInput
				value={row.group}
				placeholder="—"
				oninput={(e) => controller.updateField(row.index, 'group', e.currentTarget.value)}
			/>
		</label>

		<dl class="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
			<dt class="text-muted-foreground">Source</dt>
			<dd class="truncate text-right" title={row.source}>{row.source || '—'}</dd>
			<dt class="text-muted-foreground">Confidence</dt>
			<dd class="text-right tabular-nums">{row.confidence?.toFixed(2) ?? '—'}</dd>
			<dt class="text-muted-foreground">Uncertainty</dt>
			<dd class="text-right tabular-nums">{row.uncertainty?.toFixed(2) ?? '—'}</dd>
		</dl>

		<!-- RELATIONS. Only rendered when the task DECLARES one: a link rail on a task with no
		     relations is a control that can produce nothing, which reads as broken rather than as
		     inapplicable. -->
		{#if controller.relationNames.length > 0}
			<div class="flex flex-col gap-2 border-t pt-3" data-testid="relations-panel">
				<span class="text-muted-foreground text-xs font-medium">Relations</span>
				<div class="flex flex-wrap gap-1">
					{#each controller.relationNames as name (name)}
						<Button
							variant={controller.linkMode === name ? 'secondary' : 'outline'}
							size="sm"
							aria-pressed={controller.linkMode === name}
							data-testid="link-mode-{name}"
							title="Link this annotation to another — then click the target"
							onclick={() => controller.toggleLinkMode(name)}
						>
							<Link2 class="size-3.5" />
							{name}
						</Button>
					{/each}
				</div>
				{#if controller.linkMode}
					<!-- The armed state SAYS what it is waiting for. Without this the mode is invisible
					     and the next click looks like it did nothing. -->
					<p class="text-muted-foreground text-xs" data-testid="link-hint">
						{controller.pendingLinkFrom
							? 'Now click the target annotation on the canvas.'
							: 'Click the source annotation.'}
					</p>
				{/if}
				{#each links as link (link.name + link.from_shape + link.to_shape)}
					<div class="flex items-center justify-between gap-2 text-xs">
						<span class="truncate">
							<Badge variant="outline">{link.name}</Badge>
							<span class="text-muted-foreground font-mono">{endLabel(link, row.id)}</span>
						</span>
						<Button
							variant="ghost"
							size="icon-sm"
							title="Remove this link"
							data-testid="unlink"
							onclick={() => controller.removeLink(link)}
						>
							<Link2Off class="size-3.5" />
						</Button>
					</div>
				{/each}
			</div>
		{/if}

		<div class="flex gap-1">
			<Button
				variant="outline"
				size="sm"
				class="flex-1"
				title="Accept &amp; advance (A / Enter)"
				onclick={() => controller.acceptAndAdvance('accepted')}
			>
				<Check class="size-3.5" /> Accept
			</Button>
			<Button
				variant="outline"
				size="sm"
				class="flex-1"
				title="Reject &amp; advance (R)"
				onclick={() => controller.acceptAndAdvance('rejected')}
			>
				<X class="size-3.5" /> Reject
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				title="Reset to prediction"
				onclick={() => controller.setStatus(row.index, 'prediction')}
			>
				<RotateCcw class="size-3.5" />
			</Button>
		</div>
	</div>
{/if}
