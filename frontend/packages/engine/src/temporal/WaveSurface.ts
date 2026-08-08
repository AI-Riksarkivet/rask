/**
 * WaveSurface — the audio TEMPORAL surface, framework-agnostic (no Svelte).
 *
 * Wraps wavesurfer.js v7 + its RegionsPlugin behind the same shape the spatial
 * engine exposes: rows are the source of truth (`setSegments`), user gestures are
 * reported as intents (`onCreate`/`onResize`/`onSelect`) for the annotator facade to
 * turn into inserts/edits. The waveform is the ONE Canvas2D lane we allow — everything
 * else stays WebGPU. Reusable outside Svelte + testable; the `.svelte` viewer is a thin
 * mount wrapper. See RA_ANNO_MERGE.md §5c–5d.
 */
import WaveSurfer from 'wavesurfer.js';
import RegionsPlugin from 'wavesurfer.js/dist/plugins/regions.js';

/** A time-range annotation as the surface renders it (seconds). */
export interface TemporalSegment {
	id: string;
	start: number;
	end: number;
	label?: string;
	color?: string;
}

/** Gesture intents — the facade decides what they mean (insert / edit / select). */
export interface WaveSurfaceCallbacks {
	/** User drag-selected a new range on the waveform. */
	onCreate?: (seg: { start: number; end: number }) => void;
	/** User resized/moved an existing region (drag-end), keyed by its row id. */
	onResize?: (id: string, start: number, end: number) => void;
	/** User clicked a region (its id) or empty waveform (null). */
	onSelect?: (id: string | null) => void;
	/** Decode finished — the total duration is known. */
	onReady?: (duration: number) => void;
	/** Playback started/stopped — drives a play/pause button. */
	onPlayStateChange?: (playing: boolean) => void;
	/** The playhead moved — drives the transport's time readout.
	 *
	 *  Needed because the surface owned the only knowledge of WHERE playback was: the audio
	 *  annotator's entire control set was one play/pause button, with no way to see or say what
	 *  moment a segment boundary sat at. */
	onTimeUpdate?: (seconds: number) => void;
	onError?: (err: unknown) => void;
}

export interface WaveSurfaceOptions {
	/** Audio source (the `<audio>` URL). */
	url: string;
	/** Bind to an EXISTING media element (a video's own soundtrack) instead of letting wavesurfer
	 *  create an `<audio>`: playback, seeking and the playhead stay in lockstep with that element,
	 *  so a waveform lane under a video needs no second transport. */
	media?: HTMLMediaElement;
	/** Precomputed peaks (+ `duration`) render WITHOUT decoding — the video lane's fallback for a
	 *  clip whose audio cannot be decoded (or that has no audio track at all): a flat timeline
	 *  whose REGIONS still work, because labeling events on a silent clip is still labeling. */
	peaks?: number[][];
	duration?: number;
	waveColor?: string;
	progressColor?: string;
	regionColor?: string;
	/** Drag-to-create + resize/move enabled (annotator edit mode). */
	editable?: boolean;
	height?: number;
}

const DEFAULTS = {
	waveColor: '#94a3b8',
	progressColor: '#3b82f6',
	regionColor: 'rgba(59,130,246,0.22)',
	height: 96,
} as const;

export class WaveSurface {
	private readonly ws: WaveSurfer;
	private readonly regions: RegionsPlugin;
	private readonly cb: WaveSurfaceCallbacks;
	private readonly regionColor: string;
	private disableDrag: (() => void) | null = null;
	// Guards our own programmatic region churn (clear/add/setOptions) from echoing back
	// through the user-gesture handlers.
	private syncing = false;
	private destroyed = false;

	constructor(container: HTMLElement, opts: WaveSurfaceOptions, cb: WaveSurfaceCallbacks = {}) {
		this.cb = cb;
		this.regionColor = opts.regionColor ?? DEFAULTS.regionColor;
		this.regions = RegionsPlugin.create();
		this.ws = WaveSurfer.create({
			container,
			url: opts.url,
			...(opts.media ? { media: opts.media } : {}),
			...(opts.peaks ? { peaks: opts.peaks } : {}),
			...(opts.duration != null ? { duration: opts.duration } : {}),
			height: opts.height ?? DEFAULTS.height,
			waveColor: opts.waveColor ?? DEFAULTS.waveColor,
			progressColor: opts.progressColor ?? DEFAULTS.progressColor,
			cursorColor: opts.progressColor ?? DEFAULTS.progressColor,
			plugins: [this.regions],
		});

		this.ws.on('ready', (duration) => this.cb.onReady?.(duration));
		this.ws.on('play', () => this.cb.onPlayStateChange?.(true));
		this.ws.on('pause', () => this.cb.onPlayStateChange?.(false));
		this.ws.on('finish', () => this.cb.onPlayStateChange?.(false));
		this.ws.on('error', (err) => this.cb.onError?.(err));
		this.ws.on('timeupdate', (t) => this.cb.onTimeUpdate?.(t));

		this.regions.on('region-created', (region) => {
			if (this.syncing) return; // our own addRegion — not a user gesture
			const start = region.start;
			const end = region.end || start + 0.5;
			region.remove(); // drop the temp; the new row round-trips back via setSegments
			this.cb.onCreate?.({ start, end });
		});
		this.regions.on('region-updated', (region) => {
			if (this.syncing) return;
			this.cb.onResize?.(region.id, region.start, region.end);
		});
		this.regions.on('region-clicked', (region, e) => {
			e.stopPropagation(); // don't let the click also seek the empty waveform
			this.ws.setTime(region.start);
			this.cb.onSelect?.(region.id);
		});
		// A click on the bare waveform (seek) clears the selection.
		this.ws.on('interaction', () => this.cb.onSelect?.(null));

		this.setEditable(opts.editable ?? false);
	}

	/** Toggle drag-to-create + per-region drag/resize (annotator view↔edit mode). */
	setEditable(editable: boolean): void {
		if (this.destroyed) return;
		this.disableDrag?.();
		this.disableDrag = editable
			? this.regions.enableDragSelection({ color: this.regionColor })
			: null;
		this.syncing = true;
		for (const r of this.regions.getRegions()) r.setOptions({ drag: editable, resize: editable });
		this.syncing = false;
	}

	/** Reconcile the displayed regions to the rows (the source of truth): clear + re-add
	 *  under the sync guard so programmatic adds don't fire as user-created regions. */
	setSegments(segs: readonly TemporalSegment[], editable: boolean): void {
		if (this.destroyed) return;
		this.syncing = true;
		this.regions.clearRegions();
		for (const s of segs) {
			this.regions.addRegion({
				id: s.id,
				start: s.start,
				end: s.end,
				content: s.label || undefined,
				color: s.color ?? this.regionColor,
				drag: editable,
				resize: editable,
			});
		}
		this.syncing = false;
	}

	/** Move the playhead + highlight a segment (sidebar → waveform). */
	focusSegment(id: string): void {
		const r = this.regions.getRegions().find((x) => x.id === id);
		if (r) this.ws.setTime(r.start);
	}

	/** Horizontal zoom, in pixels per second.
	 *
	 *  The control a waveform is USELESS for annotation without: at fit-to-width, a three-minute
	 *  recording gives roughly four pixels per second, so a word is sub-pixel and a boundary cannot
	 *  be placed, let alone nudged. wavesurfer has supported this all along — it was simply never
	 *  exposed past this wrapper. */
	zoom(pxPerSec: number): void {
		this.ws.zoom(Math.max(1, pxPerSec));
	}

	/** Playback speed. Transcription work lives at 0.75x and review at 1.5x-2x. */
	setPlaybackRate(rate: number): void {
		this.ws.setPlaybackRate(rate);
	}

	get currentTime(): number {
		return this.ws.getCurrentTime();
	}

	get duration(): number {
		return this.ws.getDuration();
	}

	seekTo(seconds: number): void {
		this.ws.setTime(seconds);
	}

	playPause(): void {
		void this.ws.playPause();
	}

	destroy(): void {
		if (this.destroyed) return;
		this.destroyed = true;
		this.disableDrag?.();
		this.ws.destroy();
	}
}
