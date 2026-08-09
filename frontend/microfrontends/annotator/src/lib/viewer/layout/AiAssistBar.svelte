<script lang="ts">
	// ONE entry point for everything model-driven — the rainbow button — and a panel that tells
	// the truth about WHAT will answer.
	//
	// Two facts drive what the panel offers, and both were previously invisible in it:
	//
	//  · THE REGISTRY. Which producers exist, which are LIVE vs answering from the honest mock,
	//    what each returns, and whether that output fits the open task — the service computes all
	//    of it, and the panel now wears it per row instead of hiding it in a settings popover.
	//    A prompt-driven producer is model-dependent, not modality-dependent: a VLM backend can
	//    label any media from text, so Detect stays on every modality and NAMES its backend.
	//  · THE MEDIA. Region-driven producers need a canvas to draw the region ON — image or a
	//    video frame. On a text or audio unit the section says why it is absent instead of
	//    offering a tool that cannot work there.
	import { onDestroy, onMount } from 'svelte';
	import { FlaskConical, MousePointerClick, Sparkles, X } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import * as Popover from '@rask/ui/popover';
	import * as Tooltip from '@rask/ui/tooltip';
	import { RainbowButton } from '@rask/ui/rainbow-button';
	import { cn } from '@rask/ui/utils';
	import TextInput from '$lib/ui/TextInput.svelte';
	import { assistProducers, type ProducerInfo } from '../remote/assist.remote';
	import AssistRegistry from './AssistRegistry.svelte';
	import PropagatePanel from './PropagatePanel.svelte';
	import type { MediaKind } from '../types';
	import type { AnnotatorController } from '../annotator.svelte';

	let {
		controller,
		taskId = null,
		kind = 'image',
	}: { controller: AnnotatorController; taskId?: string | null; kind?: MediaKind } = $props();

	let open = $state(false);
	let prompt = $state('');
	/** The registry rows, verbatim — the panel renders FROM these, so it cannot disagree with
	 *  what actually answers. Fetched WITH the task id so per-producer compatibility computes. */
	let producers = $state<ProducerInfo[]>([]);

	// Region prompts need a canvas to draw ON — image, or a video frame.
	const spatial = $derived(kind === 'image' || kind === 'video');
	/** An unreachable registry must not empty the panel: the two built-in families answer from
	 *  the honest mock regardless, so they are the floor the panel never drops below. */
	const FALLBACK: ProducerInfo[] = [
		{ name: 'grounding-dino', configured: false, returns: ['bbox'], compatible: null },
		{ name: 'sam', configured: false, returns: ['polygon'], compatible: null },
	];
	const rows = $derived(producers.length > 0 ? producers : FALLBACK);
	const detectInfo = $derived(rows.find((p) => p.name === 'grounding-dino') ?? null);
	const regionProducers = $derived(
		rows.filter((p) => p.name !== 'grounding-dino' && p.interactive !== false),
	);

	// HONEST MOCK: until a real model runner is deployed, the backend answers assist calls with a
	// deterministic mock — the shapes LOOK real. FAIL-HONEST: mock is the stack's default state,
	// so the chip shows until the service CONFIRMS a real runner.
	let assistMocked = $state(true);
	onMount(async () => {
		try {
			const result = await assistProducers(taskId ?? null);
			if (!result.ok) return; // unreachable — keep the fail-honest mock chip
			assistMocked = !result.data.producers.some((p) => p.configured);
			producers = result.data.producers;
		} catch {
			// unreachable — keep the fail-honest mock chip
		}
	});

	/** Arm a region-driven producer and CLOSE the panel — the canvas is where a region is drawn,
	 *  and a panel floating over it would cover the thing being segmented. `sam` routes as
	 *  `sam-click` (the backend registry resolves by longest prefix). */
	function arm(producer: string): void {
		controller.setAssistProducer(producer === 'sam' ? 'sam-click' : producer);
		controller.setTool('rect');
		open = false;
	}
	const armedAs = (name: string): boolean =>
		controller.assistProducer === (name === 'sam' ? 'sam-click' : name);
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

{#snippet liveDot(p: ProducerInfo)}
	<!-- Live vs mocked, PER producer — the "which ML backend serves this" fact, in the panel. -->
	<span
		class={cn('size-1.5 shrink-0 rounded-full', p.configured ? 'bg-success' : 'bg-warning')}
		data-live={p.configured}
	></span>
{/snippet}

{#snippet fitBadge(p: ProducerInfo)}
	{#if p.compatible !== null && p.compatible !== undefined}
		<Badge variant={p.compatible ? 'secondary' : 'destructive'} class="text-[9px]">
			{p.compatible ? 'fits task' : 'off-contract'}
		</Badge>
	{/if}
{/snippet}

<div class="flex shrink-0 items-center gap-1.5" data-testid="ai-assist">
	<Popover.Root bind:open>
		<Popover.Trigger>
			{#snippet child({ props })}
				<!-- No tooltip HERE: the label already says what it is, and stacking a tooltip
				     trigger's props onto the popover trigger's clobbers the click handler. -->
				<RainbowButton {...props} data-testid="ai-assist-open">
					<Sparkles class="size-3.5" /> AI assist
				</RainbowButton>
			{/snippet}
		</Popover.Trigger>
		<Popover.Content class="w-80 p-0" data-testid="ai-assist-panel">
			<div class="flex flex-col">
				<!-- PROMPT-driven: model-dependent, not modality-dependent — a VLM labels any media
				     from text. The section names the backend that will answer. -->
				<div class="flex flex-col gap-2 p-3" data-testid="assist-detect">
					<span class="text-muted-foreground flex items-center gap-1.5 text-xs font-medium">
						Detect from a prompt
						{#if detectInfo}
							{@render liveDot(detectInfo)}
							<span class="font-mono text-[10px]">
								{detectInfo.name}{detectInfo.configured ? '' : ' · mocked'}
							</span>
							{@render fitBadge(detectInfo)}
						{/if}
					</span>
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

				<!-- REGION-driven: needs a canvas to draw ON. Absent WITH A REASON off it. -->
				{#if spatial}
					<div class="border-border flex flex-col gap-2 border-t p-3" data-testid="assist-region">
						<span class="text-muted-foreground text-xs font-medium"> Segment from a click or box </span>
						<div class="flex flex-wrap gap-1">
							{#each regionProducers as p (p.name)}
								<Button
									variant={armedAs(p.name) ? 'secondary' : 'outline'}
									size="sm"
									aria-pressed={armedAs(p.name)}
									data-testid={p.name === 'sam' ? 'arm-segment' : `arm-${p.name}`}
									onclick={() => arm(p.name)}
								>
									<MousePointerClick class="size-3.5" />
									{p.name === 'sam' ? 'Segment' : p.name}
									{@render liveDot(p)}
									{#if p.returns.length}
										<span class="text-muted-foreground text-[9px]">→ {p.returns.join('/')}</span>
									{/if}
									{@render fitBadge(p)}
								</Button>
							{/each}
						</div>
					</div>
				{:else}
					<p
						class="border-border text-muted-foreground border-t p-3 text-[11px]"
						data-testid="assist-region-absent"
					>
						Region tools apply to image and video — a {kind} item is labeled on its own surface.
					</p>
				{/if}

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
		<Tooltip.Root>
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<Badge {...props} variant="warning" data-testid="assist-mock-chip">
						<FlaskConical class="size-3" /> mocked — needs runner
					</Badge>
				{/snippet}
			</Tooltip.Trigger>
			<Tooltip.Content class="max-w-64">
				No model runner is deployed (MEDIA_ASSIST_URL unset) — assist returns deterministic mock shapes,
				not model predictions.
			</Tooltip.Content>
		</Tooltip.Root>
	{/if}
</div>
