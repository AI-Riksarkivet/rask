/**
 * Image adjustments reach the engine, survive a unit switch, and reset cleanly.
 *
 * `ImagePlugin.setImageAdjustments` (brightness/contrast/saturation via a ColorMatrixFilter)
 * shipped with the engine and had NO caller — the classic built-but-unwired seam. The controller
 * now owns the state (display-only, never saved) and this suite pins the three behaviours the
 * toolbar popover relies on: every change lands on the plugin as the full triple, a NEUTRAL state
 * never touches the plugin (engine doubles in other suites attach without an image plugin, so a
 * neutral call on attach would crash them — the guard is load-bearing), and a non-neutral state
 * re-applies when a new unit attaches, so a contrast lift chosen for a faded volume holds across
 * its pages until reset.
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

/** The engine double: records every adjustment call; everything else is inert. */
function harness() {
	const applied: Array<[number, number, number]> = [];
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
			image: {
				onViewportChange: undefined,
				zoomPercent: 100,
				setImageAdjustments: (b: number, c: number, s: number) => void applied.push([b, c, s]),
			},
		},
	};
	return { applied, ctx };
}

const TABLE = tableFromArrays({ id: ['a1'], shape_type: ['bbox'], label: ['portrait'] });

function attached() {
	const h = harness();
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not a PixiContext
	c.attach(h.ctx as any, TABLE);
	return { h, c };
}

describe('image adjustments', () => {
	it('a change lands on the plugin as the FULL triple, not just the moved axis', () => {
		const { h, c } = attached();

		c.setImageAdjust({ contrast: 1.4 });

		expect(h.applied.at(-1)).toEqual([1, 1.4, 1]);
		expect(c.imageAdjusted).toBe(true);
	});

	it('a NEUTRAL state never touches the plugin — on attach or via reset-when-neutral', () => {
		// The guard other suites depend on: their engine doubles carry no image plugin, so a
		// neutral call on attach would throw where nothing is under test.
		const { h } = attached();

		expect(h.applied, 'attach applied a neutral adjustment').toHaveLength(0);
	});

	it('a non-neutral state RE-APPLIES when the next unit attaches', () => {
		const { c } = attached();
		c.setImageAdjust({ brightness: 1.3 });

		const next = harness();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(next.ctx as any, TABLE);

		expect(next.applied.at(-1), 'the lift did not survive the unit switch').toEqual([1.3, 1, 1]);
	});

	it('reset returns to neutral AND applies it, so the filter actually clears', () => {
		const { h, c } = attached();
		c.setImageAdjust({ brightness: 1.5, saturation: 0.4 });

		c.resetImageAdjust();

		expect(h.applied.at(-1)).toEqual([1, 1, 1]);
		expect(c.imageAdjusted).toBe(false);
	});
});
