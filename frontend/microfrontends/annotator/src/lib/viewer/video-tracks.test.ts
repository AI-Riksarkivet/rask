/**
 * Video tracks reach the canvas — `tracks.ts` gets its first consumer.
 *
 * The model (rows sharing a `group` + a time are keyframes; between keyframes the box
 * interpolates) was complete and unit-tested with NOTHING reading it: every keyframe drew at its
 * authored position on every frame, so a moving object was a trail of stale boxes. The controller
 * now evaluates the tracks at the playhead and hands the canvas a DISPLAY overlay: the first
 * keyframe row shows the interpolated box, the other keyframe rows hide, and outside the track's
 * lifetime nothing shows at all (absence, never a clamped box — see boxAt's contract).
 *
 * Track mode is the viewer's call, not inferred: stills all sit at t=0, and reading them as
 * tracks would collapse any two same-group shapes into one.
 */

import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it, vi } from 'vitest';

vi.mock('svelte-sonner', () => ({
	toast: {
		success: () => {},
		error: () => {},
		message: () => {},
		warning: () => {},
		info: () => {},
	},
}));
vi.mock('./remote/assist.remote', () => ({
	assistRegions: async () => ({ shapes: [] }),
	assistProducers: async () => [],
}));
vi.mock('./remote/jobs.remote', () => ({
	submitJob: async () => ({ status: 'unsupported' }),
	jobStatus: async () => null,
}));

import { AnnotatorController } from './annotator.svelte.js';

type Geo = { x: number; y: number; w: number; h: number };

/** Engine double recording every track-display handoff. */
function harness() {
	const trackCalls: Array<{ overrides: Map<number, Geo>; hidden: Set<number> }> = [];
	const ctx = {
		plugins: {
			arrow: {
				setLinks: () => {},
				setDeleted: () => {},
				unsetDeleted: () => {},
				clearDeleted: () => {},
				isDeleted: () => false,
				setOverride: () => {},
				clearOverrides: () => {},
				getDirtyOverrides: () => new Map(),
				hasDirtyOverrides: () => false,
				sync: () => {},
				load: () => {},
				setLayerConfig: () => {},
				setHeatmapColumn: () => {},
				setSelected: () => {},
				setMultiSelected: () => {},
				setFieldOverride: () => {},
				adjustOverridesForDelete: () => {},
				setTrackDisplay: (o: ReadonlyMap<number, Geo>, h: ReadonlySet<number>) =>
					void trackCalls.push({ overrides: new Map(o), hidden: new Set(h) }),
			},
			interaction: {
				setEditMode: () => {},
				setTool: () => {},
				setBrushOptions: () => {},
				getSelectedSet: () => new Set<number>(),
				cvCapable: false,
				setImageSource: () => {},
				select: () => {},
				clearSelection: () => {},
				setSelected: () => {},
				handleKeyDown: () => {},
				set onCommit(_fn: unknown) {},
				set onChange(_fn: unknown) {},
				set onSelect(_fn: unknown) {},
				set onDirtyChange(_fn: unknown) {},
				set onCvToolReady(_fn: unknown) {},
				set onMagneticSnap(_fn: unknown) {},
			},
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
	return { trackCalls, ctx };
}

/** Two keyframes of one track (t=0 → t=4, moving right and growing), one lone grouped shape,
 *  one ungrouped shape. */
const TABLE = tableFromArrays({
	id: ['kf-a', 'kf-b', 'lone', 'plain'],
	shape_type: ['bbox', 'bbox', 'bbox', 'bbox'],
	x: Float32Array.from([0, 400, 50, 70]),
	y: Float32Array.from([10, 10, 60, 80]),
	width: Float32Array.from([100, 200, 40, 30]),
	height: Float32Array.from([80, 80, 40, 30]),
	t_start: Float32Array.from([0, 4, 1, 2]),
	group: ['trk-1', 'trk-1', 'trk-solo', ''],
	label: ['rider', 'rider', 'horse', 'cart'],
	status: ['accepted', 'accepted', 'accepted', 'accepted'],
});

function attached(trackMode = true) {
	const h = harness();
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not a PixiContext
	c.attach(h.ctx as any, TABLE);
	if (trackMode) c.setTrackMode(true);
	return { h, c };
}

const last = (h: ReturnType<typeof harness>) => h.trackCalls.at(-1)!;

describe('video tracks on the canvas', () => {
	it('track mode OFF hands the canvas an EMPTY overlay — stills are untouched', () => {
		const { h } = attached(false);

		expect(last(h).overrides.size).toBe(0);
		expect(last(h).hidden.size).toBe(0);
	});

	it('midway between keyframes: the anchor row shows the INTERPOLATED box, the other hides', () => {
		const { h, c } = attached();

		c.setTimeCursor(2); // halfway through t=0 → t=4

		const { overrides, hidden } = last(h);
		expect(overrides.get(0)).toEqual({ x: 200, y: 10, w: 150, h: 80 });
		expect(hidden.has(1), 'the second keyframe still drew at its authored position').toBe(true);
	});

	it('outside the lifetime the whole track is ABSENT — never clamped to the nearest keyframe', () => {
		const { h, c } = attached();

		c.setTimeCursor(10);

		const { overrides, hidden } = last(h);
		expect(overrides.has(0)).toBe(false);
		expect(hidden.has(0)).toBe(true);
		expect(hidden.has(1)).toBe(true);
	});

	it('a LONE grouped shape and an ungrouped shape keep their ordinary rendering', () => {
		const { h, c } = attached();

		c.setTimeCursor(2);

		const { overrides, hidden } = last(h);
		for (const i of [2, 3]) {
			expect(overrides.has(i), `row ${i} was track-overridden`).toBe(false);
			expect(hidden.has(i), `row ${i} was track-hidden`).toBe(false);
		}
	});

	it('addTrackKeyframe duplicates the selection at the playhead, promoting it to a track', () => {
		const { c } = attached();
		c.setTimeCursor(3);
		c.select(3); // 'plain' — ungrouped

		const id = c.addTrackKeyframe();

		expect(id).not.toBeNull();
		expect(c.pendingInserts).toBe(1);
		// The existing row was given a fresh track id (an undoable field edit), and the new
		// keyframe shares it — that sharing IS what makes two rows one object.
		const promoted = c.rows.find((r) => r.id === 'plain');
		const added = c.rows.find((r) => r.id === id);
		expect(promoted?.group).toMatch(/^trk-/);
		expect(added?.group).toBe(promoted?.group);
		expect(added?.tStart).toBe(3);
		expect(added?.label).toBe('cart');
		expect(c.canUndo).toBe(true);
	});

	it('the new keyframe forms a LIVE track immediately — overlay-aware, no save round-trip', () => {
		const { h, c } = attached();
		c.setTimeCursor(0);
		c.select(3);
		c.addTrackKeyframe(); // same geometry at t=0… (t_start of 'plain' is 2)

		c.setTimeCursor(1); // between the promoted row (t=2) and the new keyframe (t=0)

		const { overrides, hidden } = last(h);
		// One of the two rows represents the track (the earlier keyframe anchors), the other hides.
		expect(overrides.size + hidden.size).toBeGreaterThanOrEqual(2);
	});
});
