/**
 * Object tracks — following one object across time.
 *
 * Reported: "Where are the real time object detection following stuff than?"
 *
 * Nothing tracked. Assist was per-FRAME (`grounding-dino` → bbox, `sam` → polygon): one call, one
 * image, one box. A box drawn on a moving object existed on that frame alone, so a 3-minute clip at
 * 25fps meant 4,500 boxes for one object.
 */

import { describe, expect, it } from 'vitest';

import { boxAt, boxesAt, isKeyframe, newTrackId, tracksFrom, type TrackRow } from './tracks';

const row = (over: Partial<TrackRow> & { id: string }): TrackRow => ({
	group: 'trk-a',
	tStart: 0,
	x: 0,
	y: 0,
	width: 0.1,
	height: 0.1,
	label: 'person',
	...over,
});

/** One object moving right and growing, keyframed at 0s and 2s. */
const MOVING: TrackRow[] = [
	row({ id: 'k1', tStart: 0, x: 0.0, y: 0.0, width: 0.1, height: 0.1 }),
	row({ id: 'k2', tStart: 2, x: 0.4, y: 0.2, width: 0.3, height: 0.3 }),
];

describe('reading rows as tracks', () => {
	it('groups shapes that share a group into ONE object', () => {
		const tracks = tracksFrom(MOVING);

		expect(tracks).toHaveLength(1);
		expect(tracks[0]!.keyframes).toHaveLength(2);
		expect(tracks[0]!.from).toBe(0);
		expect(tracks[0]!.to).toBe(2);
	});

	it('SORTS keyframes by time, whatever order the table held them in', () => {
		// Interpolation is meaningless on unsorted keyframes, and Arrow rows arrive in write order.
		const tracks = tracksFrom([MOVING[1]!, MOVING[0]!]);

		expect(tracks[0]!.keyframes.map((k) => k.t)).toEqual([0, 2]);
	});

	it('ignores a shape with NO group — a one-off annotation is not a track', () => {
		// Still a perfectly valid annotation. It just belongs to one frame and has no identity over
		// time, so putting it on the timeline would invent a lifetime it does not have.
		expect(tracksFrom([row({ id: 'loose', group: '' })])).toEqual([]);
	});

	it('ignores a shape with no TIME — it cannot be placed on a timeline', () => {
		expect(tracksFrom([row({ id: 'untimed', tStart: null })])).toEqual([]);
	});

	it('ignores a shape with incomplete geometry rather than interpolating from nulls', () => {
		// A half-null box would interpolate to NaN and paint nowhere, which reads as the tool being
		// broken rather than as the row being incomplete.
		expect(tracksFrom([row({ id: 'partial', width: null })])).toEqual([]);
	});

	it('separates two different objects', () => {
		const tracks = tracksFrom([
			row({ id: 'a1', group: 'trk-a', tStart: 0 }),
			row({ id: 'b1', group: 'trk-b', tStart: 1 }),
		]);

		expect(tracks.map((t) => t.group)).toEqual(['trk-a', 'trk-b']);
	});

	it('names a track by its FIRST keyframe — a relabel is not a second object', () => {
		const tracks = tracksFrom([
			row({ id: 'k1', tStart: 0, label: 'person' }),
			row({ id: 'k2', tStart: 1, label: 'rider' }),
		]);

		expect(tracks[0]!.label).toBe('person');
	});
});

describe('where the object IS — interpolation', () => {
	const track = tracksFrom(MOVING)[0]!;

	it('returns the authored box exactly ON a keyframe', () => {
		expect(boxAt(track, 0)).toEqual({ x: 0, y: 0, width: 0.1, height: 0.1 });
		expect(boxAt(track, 2)).toEqual({ x: 0.4, y: 0.2, width: 0.3, height: 0.3 });
	});

	it('interpolates LINEARLY halfway between two keyframes', () => {
		// The whole point: the annotator marked 0s and 2s; 1s is computed. That is what makes the
		// work proportional to the object's motion instead of to the frame count.
		const b = boxAt(track, 1)!;

		expect(b.x).toBeCloseTo(0.2, 6);
		expect(b.y).toBeCloseTo(0.1, 6);
		expect(b.width).toBeCloseTo(0.2, 6);
		expect(b.height).toBeCloseTo(0.2, 6);
	});

	it('interpolates at an arbitrary fraction', () => {
		expect(boxAt(track, 0.5)!.x).toBeCloseTo(0.1, 6);
	});

	it('is NULL outside the lifetime, never clamped to the nearest keyframe', () => {
		// The rule that matters most. A clamped box asserts the object is present before it enters
		// and after it leaves — inventing labels for frames nobody annotated, which is the worst
		// thing an annotation tool can do to training data.
		expect(boxAt(track, -0.1)).toBeNull();
		expect(boxAt(track, 2.1)).toBeNull();
	});

	it('handles a SINGLE-keyframe track without dividing by zero', () => {
		const one = tracksFrom([row({ id: 'k', tStart: 1, x: 0.5 })])[0]!;

		expect(boxAt(one, 1)).toEqual({ x: 0.5, y: 0, width: 0.1, height: 0.1 });
		expect(boxAt(one, 1.0001)).toBeNull();
	});

	it('interpolates across THREE keyframes using the surrounding pair only', () => {
		// A middle keyframe must bend the path — that is how an annotator corrects a track. If
		// interpolation used only the first and last, adding a keyframe would change nothing and the
		// correction would silently do nothing.
		const bent = tracksFrom([
			row({ id: 'a', tStart: 0, x: 0 }),
			row({ id: 'b', tStart: 1, x: 0.9 }),
			row({ id: 'c', tStart: 2, x: 1 }),
		])[0]!;

		expect(boxAt(bent, 0.5)!.x).toBeCloseTo(0.45, 6);
		expect(boxAt(bent, 1.5)!.x).toBeCloseTo(0.95, 6);
	});
});

describe('what the canvas draws on the current frame', () => {
	it('returns every track alive at that moment, and only those', () => {
		const tracks = tracksFrom([
			row({ id: 'a1', group: 'a', tStart: 0 }),
			row({ id: 'a2', group: 'a', tStart: 1 }),
			row({ id: 'b1', group: 'b', tStart: 2 }),
			row({ id: 'b2', group: 'b', tStart: 3 }),
		]);

		expect(boxesAt(tracks, 0.5).map((r) => r.track.group)).toEqual(['a']);
		expect(boxesAt(tracks, 2.5).map((r) => r.track.group)).toEqual(['b']);
		expect(boxesAt(tracks, 1.5)).toEqual([]); // a has ended, b has not begun
	});
});

describe('keyframe marking', () => {
	const track = tracksFrom(MOVING)[0]!;

	it('knows an authored moment from an interpolated one', () => {
		// Editing ON a keyframe changes an authored value; editing between them creates a new one.
		// The timeline marks the difference so an annotator knows which act they are performing.
		expect(isKeyframe(track, 0)).toBe(true);
		expect(isKeyframe(track, 2)).toBe(true);
		expect(isKeyframe(track, 1)).toBe(false);
	});

	it('tolerates float drift from a frame-step seek', () => {
		// A seek lands on 2/25 = 0.08000000000000002. Exact equality would report an authored
		// keyframe as interpolated on the very frame it was drawn.
		expect(isKeyframe(track, 2 + 1e-9)).toBe(true);
	});
});

describe('track ids', () => {
	it('is random, not sequential', () => {
		// Two annotators working the same item offline would both mint `track-1`, and their tracks
		// would MERGE on save — silently, because sharing a group is exactly what makes two rows one
		// object. That is data corruption, not a cosmetic clash.
		expect(newTrackId(() => 0.5)).toBe(newTrackId(() => 0.5));
		expect(newTrackId(() => 0.1)).not.toBe(newTrackId(() => 0.9));
		expect(newTrackId()).toMatch(/^trk-[a-z0-9]+$/);
	});
});
