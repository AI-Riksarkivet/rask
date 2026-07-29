<script lang="ts">
	// Single-annotation inspector/editor. Controlled: edits route through the facade
	// (canvas + overlay updated together). (Ported from ra-anno AnnotationSidebar detail.)
	import { Check, X, RotateCcw, ChevronUp, ChevronDown } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import TextInput from '$lib/ui/TextInput.svelte';
	import { statusVariant } from './statusStyle';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	const row = $derived(controller.selected);
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
