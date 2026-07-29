<script lang="ts">
	// Interactive AI-assist bar (floating over the canvas) — two producers, one review
	// path. DETECT = GroundingDINO: type a class → boxes. SEGMENT = SAM: click or drag a
	// box → a mask. Both drop `status=prediction`, `source=model:…` rows the reviewer
	// accepts/rejects like any prediction. (ra-atr AI-labeling parity.)
	import { onDestroy, onMount } from 'svelte';
	import { FlaskConical, MousePointerClick, Sparkles } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import TextInput from '$lib/ui/TextInput.svelte';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	let prompt = $state('');
	let mode = $state<'detect' | 'segment'>('detect');

	// HONEST MOCK: until a real model runner is deployed (MEDIA_ASSIST_URL set), the
	// backend answers assist calls with a deterministic mock — the shapes LOOK real, so
	// without this chip a reviewer could mistake them for model output. Presence comes
	// from the zone's own /api/config (BFF env, never the URL itself). FAIL-HONEST:
	// mock is the stack's default state, so the chip shows until the config CONFIRMS a
	// real runner — a failed/unreachable config fetch keeps the warning up rather than
	// silently passing mock shapes off as model output.
	let assistMocked = $state(true);
	onMount(async () => {
		try {
			const res = await fetch(`${base}/api/config`);
			if (res.ok) {
				const cfg = (await res.json()) as { assistRunner?: boolean };
				assistMocked = cfg.assistRunner !== true;
			}
		} catch {
			// config unreachable — keep the fail-honest mock chip
		}
	});

	function setMode(m: 'detect' | 'segment'): void {
		mode = m;
		if (m === 'segment') {
			controller.setAssistProducer('sam-click'); // route the next draw to SAM
			controller.setTool('rect'); // click or drag a box to segment
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
