<script lang="ts">
	// Audio viewer — a THIN wrapper over the engine's WaveSurface. All wavesurfer +
	// region↔row logic lives in `@rask/engine` (framework-agnostic, reusable); this file
	// only mounts it, bridges gestures to the annotator facade via the data-only
	// (temporal) seam, and reconciles regions from the controller's rows. The waveform
	// is the one Canvas2D lane we allow; segments (t_start/t_end) share the SAME
	// annotations table + Save path as spatial shapes.
	import { untrack } from 'svelte';
	import { loadAnnotations } from '@rask/labeling/annotations-client';
	import { Pause, Play } from '@lucide/svelte';
	import { WaveSurface, type TemporalSegment } from '@rask/engine';
	import { Button } from '@rask/ui/button';
	import type { ViewerProps } from './types';

	let { unit, onload, onerror, controller }: ViewerProps = $props();

	let container = $state<HTMLDivElement | null>(null);
	let surface = $state<WaveSurface | null>(null);
	let ready = $state(false);
	let playing = $state(false);
	let error = $state<string | null>(null);

	// Rows are the source of truth. A segment = any row with a real time range; id→index
	// bridges a waveform gesture back to its table row for edits.
	const segments = $derived.by<TemporalSegment[]>(() => {
		const out: TemporalSegment[] = [];
		for (const r of controller?.rows ?? []) {
			if (r.tStart == null || r.tEnd == null || r.tEnd <= r.tStart) continue;
			out.push({ id: r.id, start: r.tStart, end: r.tEnd, label: r.label });
		}
		return out;
	});
	const indexById = $derived(new Map((controller?.rows ?? []).map((r) => [r.id, r.index])));
	const editable = $derived(controller?.mode === 'edit');

	// Share this unit's annotations with the facade WITHOUT a Pixi engine (the temporal
	// seam). Runs once per unit — the shell re-mounts this component per unit.
	$effect(() => {
		if (!controller) return;
		const url = unit.annotationsUrl;
		void loadAnnotations(url)
			.then(({ table, version }) => controller.attachData(table, url, version))
			// Unreachable annotations → the viewer stays read-only, but say so (the shell's
			// status chip) instead of failing silently.
			.catch((e: unknown) => onerror?.(e instanceof Error ? e.message : String(e)));
	});

	// Surface lifecycle — synchronous so the cleanup captures the instance. Depends only
	// on the container + audio URL, never on edit-mode/segments (those flow via methods,
	// so toggling mode never re-decodes the audio).
	$effect(() => {
		const el = container;
		const url = unit.mediaUrl;
		if (!el || !url) return;
		const s = new WaveSurface(
			el,
			{ url },
			{
				onReady: () => {
					ready = true;
					onload?.(controller?.rows.length ?? 0);
				},
				onPlayStateChange: (p) => (playing = p),
				onError: (e) => (error = e instanceof Error ? e.message : String(e)),
				onCreate: ({ start, end }) =>
					controller?.addTemporalSegment({ t_start: start, t_end: end }),
				onResize: (id, start, end) => {
					const i = indexById.get(id);
					if (i != null) controller?.updateSegmentTime(i, start, end);
				},
				onSelect: (id) => controller?.select(id == null ? null : (indexById.get(id) ?? null)),
			},
		);
		surface = s;
		return () => {
			s.destroy();
			surface = null;
			ready = false;
		};
	});

	// Rows → regions (editable read untracked so a mode toggle doesn't re-add regions —
	// setEditable handles that below).
	$effect(() => {
		if (surface && ready)
			surface.setSegments(
				segments,
				untrack(() => editable),
			);
	});
	$effect(() => surface?.setEditable(editable));
	// Sidebar selection → move the playhead to that segment.
	$effect(() => {
		const sel = controller?.selected;
		if (sel && surface) surface.focusSegment(sel.id);
	});
</script>

<div class="flex h-full w-full flex-col gap-3 p-4">
	<div class="flex items-center gap-2">
		<Button
			variant="outline"
			size="sm"
			disabled={!ready}
			onclick={() => surface?.playPause()}
			aria-label={playing ? 'Pause' : 'Play'}
		>
			{#if playing}
				<Pause class="size-4" />
			{:else}
				<Play class="size-4" />
			{/if}
		</Button>
		<span class="text-muted-foreground text-xs">
			{editable ? 'Drag on the waveform to add a segment' : 'Audio segments'} · {segments.length}
		</span>
	</div>

	<div
		class="border-border bg-card min-h-[96px] w-full rounded-md border p-2"
		bind:this={container}
	></div>

	{#if error}
		<p class="text-destructive text-xs">Failed to load audio: {error}</p>
	{:else if !ready}
		<p class="text-muted-foreground text-xs">Loading waveform…</p>
	{/if}
</div>
