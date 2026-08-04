/**
 * Text spans — a range INTO another annotation's text.
 *
 * This is what token-classification and the value half of KIE on transcribed text ARE, and no amount
 * of pixel geometry expresses one. The interesting properties are the ones a wrong implementation
 * still looks right for: a span is addressed by parent ID (rows renumber), it stores OFFSETS (text
 * repeats within a line), and it is REFUSED rather than clamped when it does not fit.
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
	assistProducers: async () => ({ ok: true, data: { producers: [], default_configured: false } }),
}));
vi.mock('./remote/jobs.remote', () => ({
	submitJob: async () => ({ status: 'unsupported' }),
	jobStatus: async () => null,
}));

import { AnnotatorController } from './annotator.svelte.js';

const noop = () => {};
function ctx() {
	return {
		plugins: {
			arrow: {
				setLinks: noop,
				setDeleted: noop,
				unsetDeleted: noop,
				clearDeleted: noop,
				isDeleted: () => false,
				setOverride: noop,
				clearOverrides: noop,
				getDirtyOverrides: () => new Map(),
				hasDirtyOverrides: () => false,
				sync: noop,
				load: noop,
				setLayerConfig: noop,
				setHeatmapColumn: noop,
				setSelected: noop,
				setMultiSelected: noop,
				setFieldOverride: noop,
				adjustOverridesForDelete: noop,
			},
			interaction: {
				setEditMode: noop,
				setTool: noop,
				setBrushOptions: noop,
				getSelectedSet: () => new Set<number>(),
				cvCapable: false,
				setImageSource: noop,
				select: noop,
				clearSelection: noop,
				setSelected: noop,
				handleKeyDown: noop,
				set onCommit(_f: unknown) {},
				set onChange(_f: unknown) {},
				set onSelect(_f: unknown) {},
				set onDirtyChange(_f: unknown) {},
				set onCvToolReady(_f: unknown) {},
				set onMagneticSnap(_f: unknown) {},
			},
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
}

const LINE = 'Gustav Vasa was crowned in 1523';
const TABLE = tableFromArrays({
	id: ['l1'],
	shape_type: ['bbox'],
	label: ['line'],
	text: [LINE],
});

function attached() {
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
	c.attach(ctx() as any, TABLE, '/save', 1);
	return c;
}

describe('text spans', () => {
	it('creates a row addressed by the PARENT ID, not its index', () => {
		// Rows renumber on every reload, so an index would silently re-point at a different
		// annotation. The id is the only stable handle.
		const c = attached();
		c.addTextSpan(0, 0, 11, 'entity');

		expect(c.pendingInserts).toBe(1);
		expect(c.rows.at(-1)!.id).not.toBe('l1');
		expect(c.rows.map((r) => r.label)).toContain('entity');
	});

	it('stores the SUBSTRING for readability but the range is what is authoritative', () => {
		const c = attached();
		c.addTextSpan(0, 0, 11, 'entity');
		expect(c.rows.at(-1)!.text).toBe('Gustav Vasa');
	});

	it('REFUSES a range past the end of its parent, rather than clamping', () => {
		// A clamped span is a range nobody asked for — and unlike a clamped box, you cannot see that
		// it is wrong by looking at the page.
		const c = attached();
		c.addTextSpan(0, 0, 999, 'entity');
		expect(c.pendingInserts, 'an out-of-range span was clamped into existence').toBe(0);
	});

	it('REFUSES an empty or reversed range', () => {
		const c = attached();
		c.addTextSpan(0, 5, 5, 'entity');
		c.addTextSpan(0, 9, 3, 'entity');
		c.addTextSpan(0, -1, 4, 'entity');
		expect(c.pendingInserts).toBe(0);
	});

	it('REFUSES a span on a row that is not there', () => {
		const c = attached();
		c.addTextSpan(99, 0, 4, 'entity');
		expect(c.pendingInserts).toBe(0);
	});

	it('a span at offset ZERO is real — 0 is the first character, not "absent"', () => {
		// The trap that a truthiness check springs: dropping char_start 0 shifts every span that
		// starts at the beginning of a line.
		const c = attached();
		c.addTextSpan(0, 0, 6, 'entity');
		expect(c.pendingInserts).toBe(1);
		expect(c.rows.at(-1)!.text).toBe('Gustav');
	});

	it('a span is UNDOABLE like any other drawn row', () => {
		const c = attached();
		c.addTextSpan(0, 0, 11, 'entity');
		expect(c.canUndo).toBe(true);

		c.undo();
		expect(c.pendingInserts, 'undo left the span in the save payload').toBe(0);
	});

	it('span classes come from the TASK, never from labels already on the page', () => {
		// Observed live: with only `line` rows present, a table-derived class list offered `line` as
		// the class for a span — and a span labelled with its parent's class is meaningless.
		const c = attached();
		expect(c.textSpanClasses).toEqual([]);

		c.textSpanClasses = ['entity'];
		expect(c.textSpanClasses).toEqual(['entity']);
		expect(c.labelClasses, 'the two lists must not be the same source').toContain('line');
	});

	it('the span affordance is offered only when the task permits `text`', () => {
		const c = attached();
		expect(c.allowsTextSpans, 'an unconstrained canvas should allow spans').toBe(true);

		c.allowedShapeTypes = ['bbox'];
		expect(c.allowsTextSpans, 'a bbox-only task offered a span editor').toBe(false);

		c.allowedShapeTypes = ['bbox', 'text'];
		expect(c.allowsTextSpans).toBe(true);
	});
});
