<script lang="ts">
	// Interactive AI-assist bar (floating over the canvas) — two producers, one review
	// path. DETECT = GroundingDINO: type a class → boxes. SEGMENT = SAM: click or drag a
	// box → a mask. Both drop `status=prediction`, `source=model:…` rows the reviewer
	// accepts/rejects like any prediction. (ra-atr AI-labeling parity.)
	import { onDestroy, onMount } from 'svelte';
	import { FlaskConical, MousePointerClick, Sparkles } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import TextInput from '$lib/ui/TextInput.svelte';
	import { assistProducers } from '../remote/assist.remote';
	import AssistRegistry from './AssistRegistry.svelte';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller, taskId = null }: { controller: AnnotatorController; taskId?: string | null } =
		$props();

	let prompt = $state('');
	let mode = $state<string>('detect');
	// Registry producers beyond the two built-ins (the swap-a-model-without-code seam), asked of the
	// SERVICE. This used to read the zone's own `zoneConfig`, which re-parsed MEDIA_ASSIST_BACKENDS
	// out of THIS pod's env — a second copy of the registry that could name producers the service
	// does not have, and miss ones it does.
	let extraProducers = $state<string[]>([]);

	// HONEST MOCK: until a real model runner is deployed (MEDIA_ASSIST_URL set), the
	// backend answers assist calls with a deterministic mock — the shapes LOOK real, so
	// without this chip a reviewer could mistake them for model output.
	// FAIL-HONEST: mock is the stack's default state, so the chip shows until the service
	// CONFIRMS a real runner — a failed/unreachable read keeps the warning up rather than
	// silently passing mock shapes off as model output.
	let assistMocked = $state(true);
	onMount(async () => {
		try {
			const result = await assistProducers(null);
			if (!result.ok) return; // unreachable — keep the fail-honest mock chip
			// Mocked unless SOMETHING real is reachable. Per-producer state lives in the registry
			// panel; this chip answers the coarser question the canvas needs at a glance.
			assistMocked = !result.data.producers.some((p) => p.configured);
			// The two built-ins already have their own buttons.
			extraProducers = result.data.producers
				.filter((p) => p.name !== 'grounding-dino' && p.name !== 'sam')
				.map((p) => p.name);
		} catch {
			// unreachable — keep the fail-honest mock chip
		}
	});

	function setMode(m: string): void {
		mode = m;
		if (m === 'segment') {
			controller.setAssistProducer('sam-click'); // route the next draw to SAM
			controller.setTool('rect'); // click or drag a box to segment
		} else if (m !== 'detect') {
			// A registry producer: region-driven exactly like Segment, routed by NAME — the
			// backend's registry (longest-prefix) decides which model answers.
			controller.setAssistProducer(m);
			controller.setTool('rect');
		} else {
			controller.setAssistProducer(null);
		}
	}
	function detect(): void {
		if (prompt.trim()) void controller.assist(prompt);
	}

	// Never leave the controller armed once the bar is gone (e.g. exiting edit mode).
	onDestroy(() => controller.setAssistProducer(null));
</script>

<!-- Same floating-card treatment as PageNav and the zoom cluster (card surface, rounded-lg,
     shadow-sm) so the canvas overlays read as one family. -->
<div
	class="border-border bg-card/90 text-card-foreground pointer-events-auto absolute top-2 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1.5 rounded-lg border p-1 shadow-sm backdrop-blur"
	data-testid="ai-assist"
>
	<div class="border-border flex overflow-hidden rounded-lg border p-0.5">
		<Button
			variant={mode === 'detect' ? 'secondary' : 'ghost'}
			size="sm"
			aria-pressed={mode === 'detect'}
			onclick={() => setMode('detect')}
		>
			<Sparkles class="size-3.5" /> Detect
		</Button>
		<Button
			variant={mode === 'segment' ? 'secondary' : 'ghost'}
			size="sm"
			aria-pressed={mode === 'segment'}
			onclick={() => setMode('segment')}
		>
			<MousePointerClick class="size-3.5" /> Segment
		</Button>
		{#each extraProducers as producer (producer)}
			<Button
				variant={mode === producer ? 'secondary' : 'ghost'}
				size="sm"
				aria-pressed={mode === producer}
				title="draw a region — the {producer} backend finds everything similar"
				onclick={() => setMode(producer)}
			>
				<MousePointerClick class="size-3.5" />
				{producer}
			</Button>
		{/each}
	</div>

	{#if mode === 'detect'}
		<TextInput
			bind:value={prompt}
			placeholder="AI detect… (e.g. 'text line')"
			aria-label="AI detect prompt"
			class="h-7 w-48"
			onkeydown={(e) => e.key === 'Enter' && detect()}
		/>
		<Button size="sm" disabled={!prompt.trim() || controller.saving} onclick={detect}>Run</Button>
	{:else}
		<span class={cn('text-muted-foreground px-2 text-xs', controller.saving && 'animate-pulse')}>
			{controller.saving ? 'segmenting…' : 'Click or drag a box to segment'}
		</span>
	{/if}

	<AssistRegistry {taskId} />

	{#if assistMocked}
		<!-- The honesty chip is a real warning Badge now, so "this output is not a model" carries
		     the same weight it does anywhere else in the estate. -->
		<Badge
			variant="warning"
			data-testid="assist-mock-chip"
			title="No model runner is deployed (MEDIA_ASSIST_URL unset) — Detect/Segment return deterministic mock shapes, not model predictions."
		>
			<FlaskConical class="size-3" /> mocked — needs runner
		</Badge>
	{/if}
</div>
