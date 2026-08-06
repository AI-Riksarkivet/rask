/**
 * The TEMPORAL transport — the arithmetic behind an audio/video player's controls.
 *
 * Reported: "where is my the video and audio player and tools for annotating in video and audio?
 * Waveformer has all features for labeling and we need a good video annotation too aswell like cvat
 * and label studio".
 *
 * Measured before this: the audio surface had ONE control (play/pause) beside a waveform and a
 * segment count. Video had play/pause, `mm:ss / mm:ss`, and a seek slider. Nothing else — no skip,
 * no playback rate, no frame stepping, no keyboard. An annotator working a three-minute recording
 * had to drag a slider to reach every segment boundary, and a video annotator could not step to the
 * frame their box belongs on, which is the single most-used control in CVAT.
 *
 * This module is the ARITHMETIC only — clamping, stepping, rate cycling, formatting. It holds no
 * player handle and no DOM, so every rule below is checkable without a decoded media file, a GPU or
 * a running dev server. The components own the wiring; the decisions live here.
 */

/** Playback rates offered, in cycle order. 1 first so the control starts where everyone expects. */
export const RATES = [1, 1.5, 2, 0.5, 0.75] as const;
export type Rate = (typeof RATES)[number];

/** The next rate in the cycle. A CYCLE rather than a dropdown: rate is adjusted constantly while
 *  transcribing, and a menu costs two clicks and a hover for what should be one key. */
export function nextRate(current: number): Rate {
	const at = RATES.indexOf(current as Rate);
	return RATES[(at + 1) % RATES.length] as Rate;
}

/**
 * Seek to `seconds`, kept inside the media.
 *
 * Clamping is not politeness. `<video>.currentTime = -1` throws in some engines and silently
 * clamps in others, and wavesurfer's `seekTo` takes a 0..1 FRACTION — feeding it a negative or
 * over-length value scrolls the waveform somewhere with no annotation on it and no way back except
 * the slider. One clamp here means neither surface can be driven out of range by a key repeat.
 */
export function clampTime(seconds: number, duration: number): number {
	if (!Number.isFinite(seconds)) return 0;
	if (!Number.isFinite(duration) || duration <= 0) return Math.max(0, seconds);
	return Math.min(Math.max(seconds, 0), duration);
}

/** Skip by `delta` seconds from `at`, clamped. Negative skips backwards. */
export function skip(at: number, delta: number, duration: number): number {
	return clampTime(at + delta, duration);
}

/**
 * One FRAME forward or back.
 *
 * The control CVAT is built around: a box belongs to a frame, not to a moment, so an annotator
 * steps rather than scrubs. `fps` is an estimate — HTML5 video exposes no frame rate, and
 * `requestVideoFrameCallback` gives one only while playing — so this rounds to the nearest frame
 * boundary FIRST and then steps. Without that rounding, repeated steps drift: from 1.037s at 25fps
 * you would land on 1.077, 1.117, … and never sit on a frame boundary at all.
 */
export function stepFrame(at: number, direction: 1 | -1, fps: number, duration: number): number {
	const rate = Number.isFinite(fps) && fps > 0 ? fps : 25;
	const frame = Math.round(at * rate);
	return clampTime((frame + direction) / rate, duration);
}

/** Which frame index a moment belongs to — what the transport DISPLAYS, so an annotator can say
 *  "the box is on frame 412" and mean something another person can navigate to. */
export function frameIndex(at: number, fps: number): number {
	const rate = Number.isFinite(fps) && fps > 0 ? fps : 25;
	return Math.max(0, Math.round(at * rate));
}

/**
 * `m:ss.d` — minutes, seconds, and ONE decimal.
 *
 * The decimal is load-bearing for annotation and is why this is not `mm:ss`: audio segment
 * boundaries are routinely tenths apart, and a display that rounds them to the second shows two
 * different boundaries as the same number. Hours are not handled because a labeling item is a clip;
 * a three-hour recording is a corpus problem, not a display one.
 */
export function fmtTime(seconds: number): string {
	if (!Number.isFinite(seconds) || seconds < 0) return '0:00.0';

	// ROUND FIRST, then split. Splitting first and rounding per-part is wrong in two ways that a
	// test caught: 9.96 padded on `s < 10` and then rendered `toFixed(1)` as "10.0", giving
	// "0:010.0"; and 59.96 would have rendered "0:60.0" instead of rolling into the next minute.
	// Deriving both parts from one already-rounded total makes both impossible.
	const total = Math.round(seconds * 10) / 10;
	const m = Math.floor(total / 60);
	const s = total - m * 60;
	return `${m}:${s < 10 ? '0' : ''}${s.toFixed(1)}`;
}

/** What a keypress means to a transport, or null when the transport should ignore it.
 *
 *  Centralised so audio and video answer the SAME keys. Two surfaces that each invented their own
 *  bindings would make muscle memory wrong half the time, which is worse than having none. */
export type TransportAction =
	| { kind: 'playPause' }
	| { kind: 'skip'; delta: number }
	| { kind: 'frame'; direction: 1 | -1 }
	| { kind: 'rate' };

export function keyAction(
	key: string,
	mods: { shift?: boolean; ctrl?: boolean; meta?: boolean; alt?: boolean } = {},
): TransportAction | null {
	// Never steal a shortcut the browser or the app owns. Ctrl/Cmd combinations are save, undo,
	// find — a transport that swallowed them would break the canvas it sits under.
	if (mods.ctrl || mods.meta || mods.alt) return null;

	if (key === ' ' || key === 'Spacebar') return { kind: 'playPause' };
	// Shift+arrow = one FRAME; bare arrow = five seconds. The frame step is the finer act, so it
	// takes the modifier — matching CVAT, where the bare arrow is the coarse move.
	if (key === 'ArrowRight') return mods.shift ? { kind: 'frame', direction: 1 } : { kind: 'skip', delta: 5 };
	if (key === 'ArrowLeft') return mods.shift ? { kind: 'frame', direction: -1 } : { kind: 'skip', delta: -5 };
	if (key === 'r' || key === 'R') return { kind: 'rate' };
	return null;
}

/**
 * Should this keypress reach the transport at all?
 *
 * False while the person is typing. The canvas sits under a labelling sidebar full of text inputs,
 * and a space that plays audio instead of typing a space is the kind of defect that makes people
 * stop using keyboard shortcuts entirely.
 */
export function transportShouldHandle(target: EventTarget | null): boolean {
	const el = target as HTMLElement | null;
	if (!el || !el.tagName) return true;
	const tag = el.tagName.toLowerCase();
	if (tag === 'input' || tag === 'textarea' || tag === 'select') return false;
	return el.isContentEditable !== true;
}
