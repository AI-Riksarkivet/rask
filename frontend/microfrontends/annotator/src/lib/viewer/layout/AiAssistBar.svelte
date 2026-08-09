<script lang="ts">
	// ONE entry point for everything model-driven — the rainbow button.
	//
	// Reported: "all of them should be put in a component and have a button you press that says
	// ai-assist". The bar used to lay its internals across the toolbar — Detect and Segment as
	// sibling buttons, a prompt input beside them, Run after that, extra producers as more
	// buttons, and Propagate parked in the RIGHT SIDEBAR as if it were a setting. Five fragments
	// of one act ("ask a model") competing with the drawing tools for a row of space.
	//
	// Now: one deliberately loud button (the estate's ONLY rainbow affordance — loud because it
	// summons a model, quiet buttons draw) opening one panel with every AI tool grouped by what it
	// needs from you: a PROMPT (detect), a REGION (segment + interactive producers — arming closes
	// the panel, the canvas is where a region is drawn), or EXEMPLARS (few-shot propagation, fed
	// by the current selection). The honest-mock chip stays OUTSIDE on the toolbar: a warning that
	// only shows once a panel is opened is a warning most people never see.
	import { onDestroy, onMount } from 'svelte';
	import { FlaskConical, MousePointerClick, Sparkles, X } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import * as Popover from '@rask/ui/popover';
	import { RainbowButton } from '@rask/ui/rainbow-button';
	import { cn } from '@rask/ui/utils';
	import TextInput from '$lib/ui/TextInput.svelte';
	import { assistProducers } from '../remote/assist.remote';
	import AssistRegistry from './AssistRegistry.svelte';
	import PropagatePanel from './PropagatePanel.svelte';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller, taskId = null }: { controller: AnnotatorController; taskId?: string | null } =
		$props();

	let open = $state(false);
	let prompt = $state('');
	// Registry producers beyond the two built-ins (the swap-a-model-without-code seam), asked of
	// the SERVICE — the only source that cannot disagree with what actually answers.
	let extraProducers = $state<string[]>([]);

	// HONEST MOCK: until a real model runner is deployed, the backend answers assist calls with a
	// deterministic mock — the shapes LOOK real. FAIL-HONEST: mock is the stack's default state,
	// so the chip shows until the service CONFIRMS a real runner.
	let assistMocked = $state(true);
	onMount(async () => {
		try {
			const result = await assistProducers(null);
			if (!result.ok) return; // unreachable — keep the fail-honest mock chip
			assistMocked = !result.data.producers.some((p) => p.configured);
			// The built-ins have their own controls; BATCH-ONLY families are jobs-seam producers
			// the interactive POST cannot reach — the registry's `interactive` flag gates them out.
			extraProducers = result.data.producers
				.filter((p) => p.name !== 'grounding-dino' && p.name !== 'sam' && p.interactive !== false)
				.map((p) => p.name);
		} catch {
			// unreachable — keep the fail-honest mock chip
		}
	});

	/** Arm a region-driven producer and CLOSE the panel — the canvas is where a region is drawn,
	 *  and a panel floating over it would cover the thing being segmented. */
	function arm(producer: string): void {
		controller.setAssistProducer(producer);
		controller.setTool('rect');
		open = false;
	}
	function disarm(): void {
		controller.setAssistProducer(null);
	}
	function detect(): void {
		if (!prompt.trim()) return;
		void controller.assist(prompt);
		open = false;
	}

	// Never leave the controller armed once the bar is gone (e.g. exiting edit mode).
	onDestroy(disarm);
</script>

<div class="flex shrink-0 items-center gap-1.5" data-testid="ai-assist">
	<Popover.Root bind:open>
		<Popover.Trigger>
			{#snippet child({ props })}
				<RainbowButton {...props} data-testid="ai-assist-open" title="Every model-driven tool">
					<Sparkles class="size-3.5" /> AI assist
				</RainbowButton>
			{/snippet}
		</Popover.Trigger>
		<Popover.Content class="w-80 p-0" data-testid="ai-assist-panel">
			<div class="flex flex-col">
				<!-- PROMPT-driven: describe what to find; boxes come back as predictions. -->
				<div class="flex flex-col gap-2 p-3">
					<span class="text-muted-foreground text-xs font-medium">Detect from a prompt</span>
					<div class="flex items-center gap-1.5">
						<TextInput
							bind:value={prompt}
							placeholder="AI detect… (e.g. 'text line')"
							aria-label="AI detect prompt"
							class="h-7 flex-1"
							onkeydown={(e) => e.key === 'Enter' && detect()}
						/>
						<Button size="sm" disabled={!prompt.trim() || controller.saving} onclick={detect}>Run</Button>
					</div>
				</div>

				<!-- REGION-driven: arm, then click or drag ON the canvas (arming closes this panel). -->
				<div class="border-border flex flex-col gap-2 border-t p-3">
					<span class="text-muted-foreground text-xs font-medium"> Segment from a click or box </span>
					<div class="flex flex-wrap gap-1">
						<Button
							variant={controller.assistProducer === 'sam-click' ? 'secondary' : 'outline'}
							size="sm"
							aria-pressed={controller.assistProducer === 'sam-click'}
							data-testid="arm-segment"
							onclick={() => arm('sam-click')}
						>
							<MousePointerClick class="size-3.5" /> Segment
						</Button>
						{#each extraProducers as producer (producer)}
							<Button
								variant={controller.assistProducer === producer ? 'secondary' : 'outline'}
								size="sm"
								aria-pressed={controller.assistProducer === producer}
								title="draw a region — the {producer} backend answers"
								onclick={() => arm(producer)}
							>
								<MousePointerClick class="size-3.5" />
								{producer}
							</Button>
						{/each}
					</div>
				</div>

				<!-- EXEMPLAR-driven: few-shot propagation over the current selection. -->
				<PropagatePanel {controller} />
			</div>
		</Popover.Content>
	</Popover.Root>

	<!-- The REGISTRY is a settings surface (which backends exist, live vs mocked), not an act —
	     it stays beside the honesty chip rather than nesting a popover inside the panel. -->
	<AssistRegistry {taskId} />

	{#if controller.assistProducer}
		<!-- The ARMED state lives on the toolbar, not in the closed panel: the next click is a
		     region prompt, and a mode nothing announces reads as a canvas that stopped drawing. -->
		<span
			class={cn(
	'text-muted-foreground flex items-center gap-1 text-xs',
	controller.saving && 'animate-pulse',
)}
			data-testid="assist-armed"
		>
			{controller.saving ? 'segmenting…' : `Click or drag a box — ${controller.assistProducer}`}
			<Button variant="ghost" size="icon-xs" title="Disarm" onclick={disarm}>
				<X class="size-3" />
			</Button>
		</span>
	{/if}

	{#if assistMocked}
		<Badge
			variant="warning"
			data-testid="assist-mock-chip"
			title="No model runner is deployed (MEDIA_ASSIST_URL unset) — assist returns deterministic mock shapes, not model predictions."
		>
			<FlaskConical class="size-3" /> mocked — needs runner
		</Badge>
	{/if}
</div>
