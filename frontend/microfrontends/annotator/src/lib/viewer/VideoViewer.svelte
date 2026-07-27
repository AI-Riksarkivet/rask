<script lang="ts">
	// Video viewer — spatial frame overlay + a transport. A THIN wrapper: it reuses the
	// ra-anno PixiJS engine (PixiCanvas + ImagePlugin + ArrowDataPlugin) unchanged — the
	// only new engine primitive is ImagePlugin.loadFromVideoFrame (a createImageBitmap
	// snapshot of the current frame). You scrub/pause the (hidden) <video>; the paused
	// frame is snapshotted UNDER the overlay so bbox/polygon/mask are drawn on the exact
	// frame, and each new shape is pinned to that moment (controller.timeCursor →
	// t_start/t_end). One annotations table + Save path with images + audio segments.
	import { onDestroy } from 'svelte';
	import { loadAnnotations } from '@rask/labeling/annotations-client';
	import { Pause, Play } from '@lucide/svelte';
	import type { PixiContext } from '@rask/engine';
	import { Button } from '@rask/ui/button';
	// @rask/ui has no slider yet, so the seek bar keeps the @rask/ui primitive — it is already
	// token-styled, so it themes with the rest of the transport.
	import { Slider } from '@rask/ui/slider';
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
			c.plugins.arrow.load(table);
			c.plugins.arrow.sync();
			// Spatial attach (this viewer HAS a canvas) — draw tools, zoom, layers all bind.
			controller?.attach(c, table, unit.annotationsUrl, version);
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

	onDestroy(() => {
		disposed = true;
		ctx = null;
	});
</script>

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

	<div class="border-border bg-card flex items-center gap-3 border-t px-3 py-2">
		<Button
			variant="outline"
			size="sm"
			disabled={!ready}
			onclick={togglePlay}
			aria-label={playing ? 'Pause' : 'Play'}
		>
			{#if playing}
				<Pause class="size-4" />
			{:else}
				<Play class="size-4" />
			{/if}
		</Button>
		<span class="text-muted-foreground w-20 shrink-0 text-xs tabular-nums">
			{fmt(currentTime)} / {fmt(duration)}
		</span>
		<Slider
			class="flex-1"
			value={currentTime}
			min={0}
			max={Math.max(duration, 0.001)}
			step={0.01}
			onValueChange={seek}
			aria-label="Seek"
		/>
	</div>
</div>
