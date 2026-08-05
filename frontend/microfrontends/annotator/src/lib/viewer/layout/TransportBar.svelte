<script lang="ts">
	// The temporal transport — ONE control bar, rendered by both the audio and the video surface.
	//
	// Reported: "where is my the video and audio player and tools for annotating in video and audio?
	// Waveformer has all features for labeling and we need a good video annotation too aswell like
	// cvat and label studio".
	//
	// Measured before this. AUDIO: one button (play/pause), a waveform, a segment count — that was
	// the entire surface. VIDEO: play/pause, `mm:ss / mm:ss`, a seek slider. To reach a segment
	// boundary you dragged a slider; to put a box on a particular frame you could not, because
	// nothing stepped frames.
	//
	// ONE component for both modalities on purpose. Two bars would drift, and the keyboard is the
	// reason it matters: an annotator's hands learn one set of keys, and a surface where space means
	// something different is worse than a surface with no shortcuts. `transport.ts` holds the
	// arithmetic so both get identical clamping, stepping and formatting.
	//
	// FRAME STEPPING is video-only, because a frame is a video concept — but the KEYS are shared, so
	// `onframe` is optional and Shift+Arrow simply does nothing on audio rather than doing something
	// different.
	import { ChevronsLeft, ChevronsRight, Gauge, Pause, Play, SkipBack, SkipForward } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';

	import { fmtTime, frameIndex } from '../transport';

	let {
		playing,
		currentTime,
		duration,
		rate,
		fps = null,
		ready = true,
		onplaypause,
		onskip,
		onframe = null,
		onrate,
		children,
	}: {
		playing: boolean;
		currentTime: number;
		duration: number;
		rate: number;
		/** Video only — enables the frame readout. Null on audio, where a frame means nothing. */
		fps?: number | null;
		ready?: boolean;
		onplaypause: () => void;
		onskip: (delta: number) => void;
		/** Video only. Absent on audio, so the frame buttons are not rendered at all rather than
		 *  rendered disabled — a control that can never work is not a disabled control. */
		onframe?: ((direction: 1 | -1) => void) | null;
		onrate: () => void;
		/** Surface-specific extras (audio's zoom, a segment count) — placed after the shared controls
		 *  so the common ones sit in the same position on both surfaces. */
		children?: import('svelte').Snippet;
	} = $props();
</script>

<div
	class="border-border bg-card flex shrink-0 flex-wrap items-center gap-1.5 border-t px-3 py-2"
	data-testid="transport-bar"
>
	<Button
		variant="outline"
		size="sm"
		disabled={!ready}
		onclick={onplaypause}
		aria-label={playing ? 'Pause' : 'Play'}
		title={playing ? 'Pause (space)' : 'Play (space)'}
		data-testid="transport-play"
	>
		{#if playing}<Pause class="size-4" />{:else}<Play class="size-4" />{/if}
	</Button>

	<Button
		variant="ghost"
		size="icon-sm"
		disabled={!ready}
		onclick={() => onskip(-5)}
		title="Back 5s (←)"
		data-testid="transport-back"
	>
		<SkipBack class="size-4" />
	</Button>
	<Button
		variant="ghost"
		size="icon-sm"
		disabled={!ready}
		onclick={() => onskip(5)}
		title="Forward 5s (→)"
		data-testid="transport-forward"
	>
		<SkipForward class="size-4" />
	</Button>

	{#if onframe}
		<!-- The control CVAT is built around: a box belongs to a FRAME, so an annotator steps rather
		     than scrubs. Rendered only where frames exist. -->
		<Button
			variant="ghost"
			size="icon-sm"
			disabled={!ready}
			onclick={() => onframe(-1)}
			title="Previous frame (Shift+←)"
			data-testid="transport-prev-frame"
		>
			<ChevronsLeft class="size-4" />
		</Button>
		<Button
			variant="ghost"
			size="icon-sm"
			disabled={!ready}
			onclick={() => onframe(1)}
			title="Next frame (Shift+→)"
			data-testid="transport-next-frame"
		>
			<ChevronsRight class="size-4" />
		</Button>
	{/if}

	<Button
		variant={rate === 1 ? 'ghost' : 'secondary'}
		size="sm"
		disabled={!ready}
		onclick={onrate}
		title="Playback speed (r)"
		data-testid="transport-rate"
	>
		<Gauge class="size-3.5" />
		{rate}×
	</Button>

	<!-- Tenths, not whole seconds: audio segment boundaries are routinely a tenth apart, and a
	     display that rounds shows two different boundaries as the same number. -->
	<span class="text-muted-foreground shrink-0 pl-1 text-xs tabular-nums" data-testid="transport-time">
		{fmtTime(currentTime)} / {fmtTime(duration)}
		{#if fps}
			<!-- The frame INDEX, so one annotator can tell another "it is on frame 412" and be
			     navigated to. -->
			· f{frameIndex(currentTime, fps)}
		{/if}
	</span>

	{@render children?.()}
</div>
