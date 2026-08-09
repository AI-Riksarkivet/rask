/**
 * Per-row attributes — the ontology's typed per-class fields, on the ordinary edit machinery.
 *
 * An attribute edit is a `metadata` field edit: the JSON object of string values rides the same
 * overlay, undo stack and Save delta as a label change. The suite pins the reading model (values
 * parse out of the row, `order` beats `y` in the lane) and the writing model (set merges, ''
 * clears, undo reaches it).
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
import { orderOf, projectRows } from './annotation-rows';
import { textLaneLines } from './text-lane';
import { rowsToShapes } from '$lib/projects/draft-shapes';

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

const TABLE = tableFromArrays({
	id: ['par', 'hdr'],
	shape_type: ['polygon', 'bbox'],
	label: ['paragraph', 'header'],
	status: ['accepted', 'accepted'],
	source: ['human', 'human'],
	metadata: ['{"order":"2"}', '{}'],
});

function attached() {
	const h = harness();
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not a PixiContext
	c.attach(h.ctx as any, TABLE);
	c.classAttributes = {
		paragraph: [
			{ name: 'order', type: 'int', choices: [], required: true },
			{ name: 'script', type: 'enum', choices: ['blackletter', 'cursive'], required: false },
		],
	};
	return { h, c };
}

describe('per-row attributes', () => {
	it("reads a row's attributes out of its metadata JSON, per its CLASS", () => {
		const { c } = attached();

		expect(c.attributesFor('paragraph').map((a) => a.name)).toEqual(['order', 'script']);
		expect(c.attributesFor('header')).toEqual([]);
		expect(c.rowAttributes(0)).toEqual({ order: '2' });
	});

	it('setAttribute MERGES into the JSON and rides the ordinary Save delta', () => {
		const { c } = attached();

		c.setAttribute(0, 'script', 'cursive');

		expect(c.rowAttributes(0)).toEqual({ order: '2', script: 'cursive' });
		expect(c.dirty).toBe(true);
		// Clearing drops the key rather than storing an empty claim.
		c.setAttribute(0, 'order', '');
		expect(c.rowAttributes(0)).toEqual({ script: 'cursive' });
	});

	it('an attribute edit is UNDOABLE like any field edit', () => {
		const { c } = attached();

		c.setAttribute(0, 'script', 'blackletter');
		expect(c.rowAttributes(0)['script']).toBe('blackletter');

		c.undo();
		expect(c.rowAttributes(0)['script']).toBeUndefined();
	});

	it('a DECLARED reading order beats geometry in the lane', () => {
		const table = tableFromArrays({
			id: ['a', 'b', 'c'],
			shape_type: ['text', 'text', 'text'],
			label: ['', '', ''],
			text: ['third by order', 'first by order', 'no order — y only'],
			parent_id: ['', '', ''],
			y: Float32Array.from([10, 500, 250]),
			status: ['accepted', 'accepted', 'accepted'],
			source: ['ingest', 'ingest', 'ingest'],
			metadata: ['{"order":"3"}', '{"order":"1"}', '{}'],
		});
		const lines = textLaneLines(projectRows(table, new Map(), []));

		// b (order 1) then a (order 3) despite a sitting HIGHER on the page; c (no order) last.
		expect(lines.map((l) => l.id)).toEqual(['b', 'a', 'c']);
	});

	it('orderOf tolerates malformed JSON', () => {
		expect(orderOf('{"order":"7"}')).toBe(7);
		expect(orderOf('not json')).toBeNull();
		expect(orderOf('{}')).toBeNull();
	});

	it('the draft snapshot carries attributes, so SUBMIT validation judges them', () => {
		const shapes = rowsToShapes(TABLE);

		expect(shapes.find((s) => s.shape_id === 'par')?.attributes).toEqual({ order: '2' });
		expect(shapes.find((s) => s.shape_id === 'hdr')?.attributes).toBeUndefined();
	});
});
