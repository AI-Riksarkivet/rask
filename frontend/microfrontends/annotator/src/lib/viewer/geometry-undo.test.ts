/**
 * Draw, delete and vertex-drag are undoable — on the SAME stack as a field edit.
 *
 * Undo used to hold `FieldEdit[]` exclusively: label, status, group and text. Drawing a shape,
 * deleting one and dragging a vertex were reversible only by reloading the page, which loses the
 * whole session. With several annotators doing hundreds of shapes a day that is work lost in week
 * one, and it is the reason an annotator stops trusting a canvas.
 *
 * One stack, so Ctrl+Z means the same thing whatever the last action was — a second stack would
 * make the meaning of undo depend on which action you happened to take last, which is worse than
 * having none.
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

/** Records what the engine was told, and models the delete overlay so undo is observable. */
function harness() {
	const hidden = new Set<number>();
	const overrides = new Map<number, unknown>();
	let commit: ((shape: Record<string, unknown>) => void) | undefined;
	let change: ((index: number, geo: unknown) => void) | undefined;
	const ctx = {
		plugins: {
			arrow: {
				// The canvas is told about links on every reload, so every engine double needs this.
				setLinks: () => {},
				setDeleted: (i: number) => void hidden.add(i),
				unsetDeleted: (i: number) => void hidden.delete(i),
				clearDeleted: () => hidden.clear(),
				isDeleted: (i: number) => hidden.has(i),
				setOverride: (i: number, geo: unknown) => void overrides.set(i, geo),
				clearOverrides: () => overrides.clear(),
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
				set onCommit(fn: (shape: Record<string, unknown>) => void) {
					commit = fn;
				},
				set onChange(fn: (index: number, geo: unknown) => void) {
					change = fn;
				},
				set onSelect(_fn: unknown) {},
				set onDirtyChange(_fn: unknown) {},
				set onCvToolReady(_fn: unknown) {},
				set onMagneticSnap(_fn: unknown) {},
			},
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
	return {
		hidden,
		overrides,
		ctx,
		draw: (shape: Record<string, unknown>) => commit?.(shape),
		drag: (index: number, geo: unknown) => change?.(index, geo),
	};
}

const TABLE = tableFromArrays({
	id: ['a1', 'a2'],
	shape_type: ['bbox', 'polygon'],
	label: ['portrait', 'stamp'],
});

function attached() {
	const h = harness();
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not a PixiContext
	c.attach(h.ctx as any, TABLE);
	return { h, c };
}

describe('geometry actions are undoable', () => {
	it('DRAW: undo removes the shape from the save payload and hides it', () => {
		const { h, c } = attached();
		h.draw({ type: 'rect', x: 1, y: 2, width: 3, height: 4 });

		expect(c.canUndo, 'drawing did not push an undo op').toBe(true);
		const afterDraw = c.pendingInserts;

		c.undo();
		expect(c.pendingInserts, 'the drawn row still rides the save payload after undo').toBe(
			afterDraw - 1,
		);
		expect(h.hidden.size, 'the drawn row is still visible after undo').toBeGreaterThan(0);

		c.redo();
		expect(c.pendingInserts, 'redo did not restore the drawn row').toBe(afterDraw);
		expect(h.hidden.size, 'redo left the row hidden').toBe(0);
	});

	it('DELETE: undo restores the row to the payload and the canvas', () => {
		const { h, c } = attached();
		c.select(0);
		c.deleteSelected();

		expect(h.hidden.has(0), 'the row was not hidden on delete').toBe(true);
		expect(c.canUndo, 'deleting did not push an undo op').toBe(true);

		c.undo();
		expect(h.hidden.has(0), 'undo did not un-hide the deleted row').toBe(false);
		expect(
			c.rows.map((r) => r.id),
			'undo did not restore the row to the list',
		).toContain('a1');

		c.redo();
		expect(h.hidden.has(0), 'redo did not re-hide the row').toBe(true);
	});

	it('VERTEX DRAG: undo drops the override so the row falls back to its own geometry', () => {
		const { h, c } = attached();
		h.drag(1, { x: 10, y: 20, w: 30, h: 40, polygon: null });

		expect(c.canUndo, 'a geometry drag did not push an undo op').toBe(true);

		c.undo();
		// `before` was null — no pending edit existed — so undo must REMOVE the override rather than
		// pin the row to a fabricated geometry.
		expect(c.pendingGeometryEdits, 'undo left a fabricated geometry override behind').toBe(0);

		c.redo();
		expect(c.pendingGeometryEdits, 'redo did not re-apply the drag').toBe(1);
		expect(h.overrides.get(1), 'redo did not push the geometry to the canvas').toMatchObject({
			x: 10,
			y: 20,
		});
	});

	it('all three share ONE stack with field edits, in order', () => {
		const { h, c } = attached();
		c.select(0);
		c.updateField(0, 'label', 'renamed');
		h.draw({ type: 'rect', x: 1, y: 2, width: 3, height: 4 });
		c.select(0);
		c.deleteSelected();

		// Undo walks back through delete → draw → field, not through three separate histories.
		c.undo();
		expect(h.hidden.has(0), 'the last undo did not reverse the DELETE').toBe(false);
		c.undo();
		expect(c.pendingInserts, 'the second undo did not reverse the DRAW').toBe(0);
		c.undo();
		expect(c.canUndo, 'the third undo did not reach the field edit').toBe(false);
	});

	it('a new action drops the redo branch', () => {
		const { h, c } = attached();
		h.draw({ type: 'rect', x: 1, y: 2, width: 3, height: 4 });
		c.undo();
		expect(c.canRedo).toBe(true);

		h.draw({ type: 'rect', x: 9, y: 9, width: 9, height: 9 });
		expect(c.canRedo, 'an undone future survived a new action').toBe(false);
	});
});
