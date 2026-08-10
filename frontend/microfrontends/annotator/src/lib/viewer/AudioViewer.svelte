<script lang="ts">
	// Audio viewer — a THIN wrapper over the engine's WaveSurface. All wavesurfer +
	// region↔row logic lives in `@rask/engine` (framework-agnostic, reusable); this file
	// only mounts it, bridges gestures to the annotator facade via the data-only
	// (temporal) seam, and reconciles regions from the controller's rows. The waveform
	// is the one Canvas2D lane we allow; segments (t_start/t_end) share the SAME
	// annotations table + Save path as spatial shapes.
	import { untrack } from 'svelte';
	import { loadAnnotations } from '@rask/labeling/annotations-client';
	import { ZoomIn, ZoomOut } from '@lucide/svelte';
	import { WaveSurface, type TemporalSegment } from '@rask/engine';
	import { Button } from '@rask/ui/button';
	import TransportBar from './layout/TransportBar.svelte';
	import {
		clampTime,
		keyAction,
		nextRate,
		skip as skipBy,
		transportShouldHandle,
	} from './transport';
	import type { ViewerProps } from './types';

	let { unit, onload, onerror, controller }: ViewerProps = $props();

	let container = $state<HTMLDivElement | null>(null);
	let surface = $state<WaveSurface | null>(null);
	let ready = $state(false);
	let playing = $state(false);
	let currentTime = $state(0);
	let duration = $state(0);
	let rate = $state(1);
	/** Pixels per second. The control a waveform is UNANNOTATABLE without: at fit-to-width a
	 *  three-minute clip gives ~4 px/s, so a word is sub-pixel and a boundary cannot be placed —
	 *  let alone nudged. 0 means fit-to-width, wavesurfer's own default and the way back to an
	 *  overview once you have zoomed in. */
	let pxPerSec = $state(0);

	function setZoom(next: number): void {
		pxPerSec = Math.min(800, Math.max(0, next));
		surface?.zoom(pxPerSec === 0 ? 1 : pxPerSec);
	}

	function setRate(): void {
		rate = nextRate(rate);
		surface?.setPlaybackRate(rate);
	}

	function seekTo(seconds: number): void {
		surface?.seekTo(clampTime(seconds, duration));
	}

	function onKeydown(e: KeyboardEvent): void {
		if (!transportShouldHandle(e.target)) return;
		const action = keyAction(e.key, {
			shift: e.shiftKey,
			ctrl: e.ctrlKey,
			meta: e.metaKey,
			alt: e.altKey,
		});
		// `frame` is deliberately unhandled: audio has no frames, and making the SAME key mean
		// something different here would be worse than it doing nothing.
		if (!action || action.kind === 'frame') return;
		e.preventDefault();
		if (action.kind === 'playPause') surface?.playPause();
		else if (action.kind === 'skip') seekTo(currentTime + action.delta);
		else setRate();
	}
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
				onTimeUpdate: (t) => (currentTime = t),
				onReady: (d) => {
					duration = d;
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

<!-- Transport keys on the WINDOW: the waveform container never holds focus, so a listener on it
     would answer only after a click. Same bindings as video — `transport.ts` owns them both. -->
<svelte:window onkeydown={onKeydown} />

<div class="flex h-full w-full flex-col gap-3 p-4">
	<!-- The waveform itself. `min-h` rather than a fixed height so a zoomed-in view still has room
	     for the region labels wavesurfer draws inside it. -->
	<div
		class="border-border bg-card min-h-[96px] w-full flex-1 overflow-x-auto rounded-md border p-2"
		bind:this={container}
	></div>

	{#if error}
		<p class="text-destructive text-xs">Failed to load audio: {error}</p>
	{:else if !ready}
		<p class="text-muted-foreground text-xs">Loading waveform…</p>
	{/if}

	<TransportBar
		{playing}
		{currentTime}
		{duration}
		{rate}
		{ready}
		onplaypause={() => surface?.playPause()}
		onskip={(d) => seekTo(currentTime + d)}
		onrate={setRate}
	>
		<!-- ZOOM is audio's own extra, and the one control that decides whether this surface can be
		     used for annotation at all. Stepped ×2 rather than a slider: you zoom to a boundary and
		     back out, and a slider makes that two gestures instead of one click. -->
		<div class="ml-2 flex shrink-0 items-center gap-1">
			<Button
				variant="ghost"
				size="icon-sm"
				disabled={!ready}
				onclick={() => setZoom(pxPerSec === 0 ? 50 : pxPerSec * 2)}
				title="Zoom in"
				data-testid="wave-zoom-in"
			>
				<ZoomIn class="size-4" />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				disabled={!ready || pxPerSec === 0}
				onclick={() => setZoom(pxPerSec <= 50 ? 0 : pxPerSec / 2)}
				title={pxPerSec === 0 ? 'Fit to width' : 'Zoom out'}
				data-testid="wave-zoom-out"
			>
				<ZoomOut class="size-4" />
			</Button>
			<span class="text-muted-foreground shrink-0 text-xs tabular-nums" data-testid="wave-zoom">
				{pxPerSec === 0 ? 'fit' : `${Math.round(pxPerSec)} px/s`}
			</span>
		</div>

		<span class="text-muted-foreground shrink-0 pl-2 text-xs" data-testid="wave-segments">
			{editable ? 'Drag on the waveform to add a segment' : 'Audio segments'} · {segments.length}
		</span>
	</TransportBar>
</div>
