/**
 * Item-level classification — the "Choices" question as tag rows, on the ordinary edit machinery.
 *
 * A chip toggle is not a new plane: ON appends a `tag` row through the same insert overlay, undo
 * stack and Save delta as a drawn shape; OFF deletes that row. The suite pins the three rules the
 * bar depends on: toggling round-trips, `activeTags` reads deletes-aware state, and undo reaches a
 * toggle exactly like it reaches a drawn box.
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

function harness() {
	const hidden = new Set<number>();
	const ctx = {
		plugins: {
			arrow: {
				setLinks: () => {},
				setDeleted: (i: number) => void hidden.add(i),
				unsetDeleted: (i: number) => void hidden.delete(i),
				clearDeleted: () => hidden.clear(),
				isDeleted: (i: number) => hidden.has(i),
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
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
	return { hidden, ctx };
}

/** One existing tag (`letter`) and one shape row — classification composes with drawing. */
const TABLE = tableFromArrays({
	id: ['tag-1', 'shape-1'],
	shape_type: ['tag', 'bbox'],
	label: ['letter', 'stamp'],
	status: ['accepted', 'accepted'],
	source: ['human', 'human'],
});

function attached() {
	const h = harness();
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not a PixiContext
	c.attach(h.ctx as any, TABLE);
	c.tagClasses = ['letter', 'envelope', 'damaged'];
	return { h, c };
}

describe('item-level classification', () => {
	it('activeTags reads the table: an existing tag row IS an active chip', () => {
		const { c } = attached();

		expect([...c.activeTags]).toEqual(['letter']);
	});

	it('toggling ON appends a tag row that rides the ordinary Save delta', () => {
		const { c } = attached();

		c.toggleTag('envelope');

		expect(c.activeTags.has('envelope')).toBe(true);
		expect(c.pendingInserts).toBe(1);
		const row = c.rows.find((r) => r.label === 'envelope');
		expect(row?.shape).toBe('tag');
		expect(row?.status).toBe('accepted');
		expect(row?.source).toBe('human');
	});

	it('toggling OFF deletes the row — including a pre-existing one', () => {
		const { c } = attached();

		c.toggleTag('letter');

		expect(c.activeTags.has('letter')).toBe(false);
		// The delete rides the same payload as any row delete.
		expect(c.dirty).toBe(true);
	});

	it('a toggle is UNDOABLE on the same stack as everything else', () => {
		const { c } = attached();

		c.toggleTag('damaged');
		expect(c.activeTags.has('damaged')).toBe(true);

		c.undo();
		expect(c.activeTags.has('damaged')).toBe(false);

		c.redo();
		expect(c.activeTags.has('damaged')).toBe(true);
	});
});
