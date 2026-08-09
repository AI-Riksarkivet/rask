<script lang="ts">
	// ITEM-LEVEL CLASSIFICATION — the Label-Studio "Choices" question, over any modality. Renders
	// when the task's ontology declares `tag` classes: one chip per choice, toggled on click; an
	// active chip IS a `tag` row (same insert overlay, undo stack, Save delta and review queue as
	// every other annotation). Multilabel by design; single-choice enforcement is the ontology
	// validator's job at submit, not the bar's.
	//
	// CVAT has no bar like this (image tags live in a sidebar); doccano/Argilla make it the whole
	// canvas for classification projects. Ours composes: over an image it sits above the canvas,
	// over text above the document, over audio above the waveform — the question follows the item.
	import { Check } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { spanColor } from '../span-render';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	const classes = $derived(controller.tagClasses);
	const active = $derived(controller.activeTags);
</script>

{#if classes.length > 0}
	<div
		class="border-sidebar-border bg-sidebar flex w-full shrink-0 flex-wrap items-center gap-1.5 border-b px-3 py-1.5"
		data-testid="classification-bar"
	>
		<span class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
			Classify
		</span>
		{#each classes as c (c)}
			<Button
				variant={active.has(c) ? 'secondary' : 'outline'}
				size="xs"
				style={`border-bottom: 2px solid ${spanColor(c, classes)}`}
				aria-pressed={active.has(c)}
				data-testid={`classify-${c}`}
				onclick={() => controller.toggleTag(c)}
			>
				{#if active.has(c)}<Check class="size-3" />{/if}
				{c}
			</Button>
		{/each}
		<span class="text-muted-foreground ml-auto text-[11px]">
			{active.size ? `${active.size} applied` : 'pick one or more'}
		</span>
	</div>
{/if}
