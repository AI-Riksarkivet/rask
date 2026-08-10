<script lang="ts">
	// Video viewer — spatial frame overlay + a transport. A THIN wrapper: it reuses the
	// ra-anno PixiJS engine (PixiCanvas + ImagePlugin + ArrowDataPlugin) unchanged — the
	// only new engine primitive is ImagePlugin.loadFromVideoFrame (a createImageBitmap
	// snapshot of the current frame). You scrub/pause the (hidden) <video>; the paused
	// frame is snapshotted UNDER the overlay so bbox/polygon/mask are drawn on the exact
	// frame, and each new shape is pinned to that moment (controller.timeCursor →
	// t_start/t_end). One annotations table + Save path with images + audio segments.
	import { onDestroy, untrack } from 'svelte';
	import { loadAnnotations } from '@rask/labeling/annotations-client';

	import { type PixiContext, type TemporalSegment, WaveSurface } from '@rask/engine';
	import { Button } from '@rask/ui/button';
	// @rask/ui has no slider yet, so the seek bar keeps the @rask/ui primitive — it is already
	// token-styled, so it themes with the rest of the transport.
	import { Slider } from '@rask/ui/slider';
	import TransportBar from './layout/TransportBar.svelte';
	import {
		clampTime,
		keyAction,
		nextRate,
		skip as skipBy,
		stepFrame,
		transportShouldHandle,
	} from './transport';
	import PixiCanvas from './PixiCanvas.svelte';
	import type { ViewerProps } from './types';

	let { unit, onload, onerror, controller }: ViewerProps = $props();

	let video = $state<HTMLVideoElement | null>(null);
	let ctx: PixiContext | null = null;
	let ready = $state(false);
	let playing = $state(false);
	let duration = $state(0);
	let currentTime = $state(0);

	let disposed = false;
	let snapping = false; // createImageBitmap is async — never overlap snapshots
	async function snapshot(): Promise<void> {
		if (disposed || snapping || !ctx || !video) return;
		snapping = true;
		try {
			await ctx.plugins.image.loadFromVideoFrame(video);
		} finally {
			snapping = false;
		}
	}

	async function onready(c: PixiContext): Promise<void> {
		ctx = c;
		try {
			const { table, version } = await loadAnnotations(unit.annotationsUrl);
			// The fetch can outlive this component (remounted per unit; onready is fired
			// un-awaited). Attaching from a stale continuation re-arms a controller whose owning
			// effect is destroyed — the `derived_inert` flood. Same guard as ImageViewer.
			if (disposed) return;
			c.plugins.arrow.load(table);
			c.plugins.arrow.sync();
			// Spatial attach (this viewer HAS a canvas) — draw tools, zoom, layers all bind.
			controller?.attach(c, table, unit.annotationsUrl, version);
			// A time axis makes same-group rows KEYFRAMES (tracks.ts) — video only, on purpose:
			// stills all sit at t=0 and would read as one-instant tracks.
			controller?.setTrackMode(true);
			onload?.(table.numRows);
		} catch (e) {
			// Honest failure over a hung "loading…" chip; not rethrown (onready is fired
			// un-awaited by PixiCanvas). The video itself may still play read-only.
			onerror?.(e instanceof Error ? e.message : String(e));
		}
		await snapshot(); // draw the first available frame under the overlay
	}

	function onLoadedData(): void {
		ready = true;
		void snapshot();
	}
	function onSeeked(): void {
		void snapshot();
	}
	function onTimeUpdate(): void {
		if (!video) return;
		currentTime = video.currentTime;
		controller?.setTimeCursor(currentTime); // pin newly-drawn shapes to this moment
	}
	function onDurationChange(): void {
		duration = video?.duration ?? 0;
	}
	function onPlay(): void {
		playing = true;
		pump();
	}
	function onPause(): void {
		playing = false;
		void snapshot(); // land on the exact paused frame
	}

	// Throttled frame pump during playback (~10fps). Annotation happens paused, so we
	// don't need a per-frame GPU upload — this just shows motion while playing.
	let lastPump = 0;
	function pump(): void {
		if (disposed || !playing || !video) return;
		const now = performance.now();
		if (now - lastPump > 100) {
			lastPump = now;
			void snapshot();
		}
		requestAnimationFrame(pump);
	}

	/** Playback rate, and an fps ESTIMATE.
	 *
	 *  HTML5 exposes no frame rate — `requestVideoFrameCallback` reports one only while playing, and
	 *  a paused annotator never triggers it. 25 is the archive's own default and is stated rather
	 *  than hidden, so a wrong step is diagnosable instead of mysterious. */
	let rate = $state(1);
	const FPS = 25;

	function setRate(): void {
		rate = nextRate(rate);
		if (video) video.playbackRate = rate;
	}

	function seekTo(seconds: number): void {
		if (!video) return;
		video.currentTime = clampTime(seconds, duration);
	}

	/** Frame stepping PAUSES first. Stepping while playing sets `currentTime` and playback
	 *  immediately overwrites it, so the frame you asked for flashes past — which reads as the
	 *  button not working. */
	function frameStep(direction: 1 | -1): void {
		if (!video) return;
		if (!video.paused) video.pause();
		seekTo(stepFrame(video.currentTime, direction, FPS, duration));
	}

	function onKeydown(e: KeyboardEvent): void {
		if (!transportShouldHandle(e.target)) return;
		const action = keyAction(e.key, {
			shift: e.shiftKey,
			ctrl: e.ctrlKey,
			meta: e.metaKey,
			alt: e.altKey,
		});
		if (!action) return;
		e.preventDefault();
		if (action.kind === 'playPause') togglePlay();
		else if (action.kind === 'skip')
			seekTo(skipBy(video?.currentTime ?? 0, action.delta, duration));
		else if (action.kind === 'frame') frameStep(action.direction);
		else setRate();
	}

	function togglePlay(): void {
		if (!video) return;
		if (video.paused) void video.play();
		else video.pause();
	}
	function seek(t: number): void {
		if (video) video.currentTime = t;
	}
	function fmt(s: number): string {
		const m = Math.floor(s / 60);
		const sec = Math.floor(s % 60);
		return `${m}:${sec.toString().padStart(2, '0')}`;
	}

	// ── the AUDIO LANE — the video's soundtrack as a labelable timeline (the CVAT/Label-Studio
	// composition: frame on top, waveform under it, transport at the bottom). Bound to the SAME
	// hidden <video>, so playback, seeking and the playhead are one clock; audio-event segments
	// ride the exact rows/Save path the audio viewer writes.
	let waveEl = $state<HTMLElement | null>(null);
	let wave = $state<WaveSurface | null>(null);
	// True when the clip's audio could not be decoded (or there is none): the lane falls back to
	// a FLAT timeline whose regions still work — labeling events on a silent clip is still
	// labeling — and says so instead of showing an empty strip that reads as broken.
	let noAudio = $state(false);

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

	function makeSurface(el: HTMLElement, opts: { peaks?: number[][]; duration?: number }) {
		return new WaveSurface(
			el,
			{ url: unit.mediaUrl ?? '', media: video ?? undefined, height: 56, ...opts },
			{
				onCreate: ({ start, end }) =>
					controller?.addTemporalSegment({ t_start: start, t_end: end }),
				onResize: (id, start, end) => {
					const i = indexById.get(id);
					if (i != null) controller?.updateSegmentTime(i, start, end);
				},
				onSelect: (id) => controller?.select(id == null ? null : (indexById.get(id) ?? null)),
				onError: () => {
					// No decodable audio — swap to the flat timeline. Guarded so a failure of the
					// FALLBACK itself cannot loop.
					if (noAudio || disposed || !waveEl) return;
					noAudio = true;
					wave?.destroy();
					wave = makeSurface(waveEl, {
						peaks: [[0, 0]],
						duration: video?.duration || duration || 1,
					});
				},
			},
		);
	}

	// Lifecycle: needs the container AND a loaded clip (the fallback needs a real duration).
	$effect(() => {
		const el = waveEl;
		if (!el || !ready || !unit.mediaUrl) return;
		const s = untrack(() => makeSurface(el, {}));
		wave = s;
		return () => {
			s.destroy();
			wave = null;
		};
	});
	// Rows → regions; mode → drag/resize. Same wiring as the audio viewer.
	$effect(() => {
		if (wave)
			wave.setSegments(
				segments,
				untrack(() => editable),
			);
	});
	$effect(() => wave?.setEditable(editable));

	onDestroy(() => {
		disposed = true;
		// Track mode belongs to THIS viewer's time axis — the next unit may be a still.
		controller?.setTrackMode(false);
		// Release the seven engine closures attach() installed over the controller; without this
		// the engine keeps writing a destroyed component's state.
		controller?.detach();
		ctx = null;
	});
</script>

<!-- The transport keys. On the WINDOW, because the canvas takes focus while drawing and the
     video element never has it — a listener on either would answer only some of the time. -->
<svelte:window onkeydown={onKeydown} />

<div class="flex h-full w-full flex-col">
	<!-- Hidden decode source: the frame is rendered through the Pixi overlay, not here. -->
	<!-- svelte-ignore a11y_media_has_caption -->
	<video
		bind:this={video}
		src={unit.mediaUrl}
		crossorigin="anonymous"
		playsinline
		preload="auto"
		class="hidden"
		onloadeddata={onLoadedData}
		onseeked={onSeeked}
		ontimeupdate={onTimeUpdate}
		ondurationchange={onDurationChange}
		onplay={onPlay}
		onpause={onPause}
	></video>

	<div class="min-h-0 flex-1">
		<PixiCanvas {onready} />
	</div>

	<!-- The soundtrack's lane: drag to label an audio event, exactly like the audio viewer —
	     same rows, same Save. Flat "timeline only" when the clip has no decodable audio. -->
	<div class="border-border bg-card relative shrink-0 border-t px-2 py-1">
		<div bind:this={waveEl} class="w-full" data-testid="video-waveform"></div>
		{#if noAudio}
			<span
				class="text-muted-foreground pointer-events-none absolute top-1.5 right-3 text-[10px]"
				data-testid="video-waveform-silent"
			>
				no audio track — timeline only
			</span>
		{/if}
	</div>

	<TransportBar
		{playing}
		{currentTime}
		{duration}
		{rate}
		{ready}
		fps={FPS}
		onplaypause={togglePlay}
		onskip={(d) => seekTo(skipBy(video?.currentTime ?? 0, d, duration))}
		onframe={frameStep}
		onrate={setRate}
	>
		<Slider
			class="ml-2 min-w-[8rem] flex-1"
			value={currentTime}
			min={0}
			max={Math.max(duration, 0.001)}
			step={0.01}
			onValueChange={seek}
			aria-label="Seek"
		/>
		<!-- The CVAT gesture: select a box, seek, add a keyframe — frames between keyframes
		     interpolate. Needs a selection; disabled says so rather than vanishing. -->
		<Button
			variant="ghost"
			size="sm"
			class="shrink-0 px-2 text-xs"
			disabled={controller?.selectedIndex == null}
			title={controller?.selectedIndex == null
	? 'Select a shape, seek to where its motion changes, then add a keyframe'
	: 'Add a keyframe for the selected object at this moment'}
			data-testid="add-keyframe"
			onclick={() => controller?.addTrackKeyframe()}
		>
			+ keyframe
		</Button>
	</TransportBar>
</div>
