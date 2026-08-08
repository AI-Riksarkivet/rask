<script lang="ts">
	// Single-annotation inspector/editor. Controlled: edits route through the facade
	// (canvas + overlay updated together). (Ported from ra-anno AnnotationSidebar detail.)
	import { Check, X, RotateCcw, ChevronUp, ChevronDown, Link2, Link2Off } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import TextInput from '$lib/ui/TextInput.svelte';
	import TextSpanSurface from './TextSpanSurface.svelte';
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
	// half of KIE on transcribed text. The TEXT ITSELF is the annotation surface (the doccano
	// interaction): spans render as class-coloured highlights in the flowing text, and selecting in
	// the surface labels a new one in place. The editable input above it stays for CORRECTING the
	// transcription — reading and labeling happen on the rendered surface, not inside a form field.
	/** The classes a span may be labelled with — the same taxonomy everything else is judged against. */
	const spanClasses = $derived(controller.textSpanClasses);
	/** This row's spans, projected for the surface — child rows addressing `row.id` by id. */
	const spanMarks = $derived(
		row
			? controller.rows
					.filter((r) => r.parentId === row.id && r.charStart != null && r.charStart >= 0)
					.map((r) => ({
						index: r.index,
						id: r.id,
						start: r.charStart ?? 0,
						end: r.charEnd ?? 0,
						label: r.label,
					}))
			: [],
	);
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
			/>
		</label>

		<!-- Offered only when the TASK declares a class that can be drawn as text. A labeling
		     surface on a task with no text class is a control that can produce nothing. -->
		{#if controller.allowsTextSpans && row.text}
			<TextSpanSurface
				text={row.text}
				marks={spanMarks}
				classes={spanClasses}
				selectedIndex={controller.selectedIndex}
				onlabel={(start, end, label) => controller.addTextSpan(row.index, start, end, label)}
				onpick={(i) => controller.select(i)}
				onremove={(i) => controller.deleteRow(i)}
			/>
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
