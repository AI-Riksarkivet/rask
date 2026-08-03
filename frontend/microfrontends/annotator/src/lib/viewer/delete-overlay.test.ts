/**
 * A delete must reach the CANVAS, not just the sidebar.
 *
 * `deleteSelected()` recorded the row id in `_deletes`, which exactly one reader consumes —
 * `projectRows`, the list/table/sidebar path. Its only canvas-directed call was
 * `interaction.handleKeyDown('Delete')`, and that was a guaranteed no-op twice over: `activeTool`
 * is null while the tool is `select` (the only mode in which you can have a selection to delete),
 * and no tool in the engine implements `'Delete'` regardless. So the row vanished from the sidebar
 * and stayed on the canvas until Save triggered a reload — one delete, two answers.
 *
 * This test drives the controller against a recording double of the engine, so it fails on the
 * SHAPE of the call rather than on pixels: with the old code `setDeleted` is never invoked and
 * `handleKeyDown` is, which is precisely the defect. A Playwright drive proves the pixels; this
 * proves the intent, and runs in milliseconds on every change.
 */

import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it, vi } from 'vitest';

// The controller imports `toast` for save feedback, and svelte-sonner pulls in a browser-only
// dependency chain that has no business in a logic test. Stub it at the module boundary rather than
// installing a DOM: what is under test here is which calls the controller makes, not what it renders.
vi.mock('svelte-sonner', () => ({
	toast: { success: () => {}, error: () => {}, message: () => {}, warning: () => {}, info: () => {} },
}));

// The controller imports the assist REMOTE function, and a `.remote.ts` module pulls in the whole
// SvelteKit server environment (`$app/server`, `$env/dynamic/private`) which does not resolve
// outside a Kit build. Stub that ONE module rather than aliasing its dependencies one at a time:
// the delete path never calls assist, so replacing it changes nothing under test while keeping the
// controller itself real.
// All three .remote.ts modules in this directory, for the same reason.
vi.mock('./remote/assist.remote', () => ({
	assistRegions: async () => ({ shapes: [] }),
	assistProducers: async () => [],
}));
vi.mock('./remote/jobs.remote', () => ({
	submitJob: async () => ({ status: 'unsupported' }),
	jobStatus: async () => null,
}));
vi.mock('./remote/config.remote', () => ({
	zoneConfig: async () => ({ assistProducers: [], assistMocked: true }),
}));

import { AnnotatorController } from './annotator.svelte.js';

/** The engine surface the controller touches on a delete, recording what it was told. */
function fakeCtx() {
	const calls: string[] = [];
	const deleted: number[] = [];
	return {
		calls,
		deleted,
		ctx: {
			plugins: {
				arrow: {
					setDeleted: (i: number) => {
						calls.push('setDeleted');
						deleted.push(i);
					},
					clearDeleted: () => void calls.push('clearDeleted'),
					sync: () => void calls.push('sync'),
					load: () => void calls.push('load'),
					clearOverrides: () => void calls.push('clearOverrides'),
					setOverride: () => void calls.push('setOverride'),
					getDirtyOverrides: () => new Map(),
					hasDirtyOverrides: () => false,
					isDeleted: (i: number) => deleted.includes(i),
					setLayerConfig: () => void calls.push('setLayerConfig'),
					setHeatmapColumn: () => {},
					setSelected: () => {},
					setMultiSelected: () => {},
					adjustOverridesForDelete: () => {},
				},
				interaction: {
					// If the controller reaches for this on a delete we want to SEE it: it was the old
					// mechanism and it never worked.
					handleKeyDown: (k: string) => void calls.push(`handleKeyDown:${k}`),
					setEditMode: () => {},
					setTool: () => {},
					setBrushOptions: () => {},
					getSelectedSet: () => new Set<number>(),
					cvCapable: false,
					setImageSource: () => {},
					select: () => void calls.push('interaction.select'),
					clearSelection: () => {},
					setSelected: () => {},
				},
				image: { onViewportChange: undefined, zoomPercent: 100 },
			},
		},
	};
}

const TABLE = tableFromArrays({
	id: ['a1', 'a2'],
	shape_type: ['bbox', 'polygon'],
	label: ['portrait', 'stamp'],
});

describe('deleting an annotation', () => {
	it('tells the CANVAS, not only the sidebar', () => {
		const harness = fakeCtx();
		const controller = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not the real PixiContext
		controller.attach(harness.ctx as any, TABLE);

		controller.select(0);
		controller.deleteSelected();

		expect(
			harness.calls,
			'the delete never reached the arrow plugin — the shape stays drawn until a reload',
		).toContain('setDeleted');
		expect(harness.deleted, 'the wrong row index was hidden').toEqual([0]);
	});

	it('re-renders immediately rather than waiting for Save', () => {
		const harness = fakeCtx();
		const controller = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		controller.attach(harness.ctx as any, TABLE);

		controller.select(1);
		controller.deleteSelected();

		// setDeleted only marks the overlay dirty; without a sync the canvas repaints on some later
		// unrelated frame, which reads as a lag rather than a delete.
		//
		// lastIndexOf, not indexOf: attach() syncs too, so the FIRST sync predates the delete and
		// comparing against it would pass even if deleteSelected never synced at all.
		expect(harness.calls.lastIndexOf('sync')).toBeGreaterThan(harness.calls.indexOf('setDeleted'));
	});

	it('does not rely on the engine keybinding, which cannot fire in select mode', () => {
		const harness = fakeCtx();
		const controller = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		controller.attach(harness.ctx as any, TABLE);

		controller.select(0);
		controller.deleteSelected();

		expect(
			harness.calls.filter((c) => c.startsWith('handleKeyDown')),
			"handleKeyDown('Delete') is a no-op: activeTool is null in select mode and no tool " +
				'implements Delete. Depending on it is how this bug survived.',
		).toEqual([]);
	});

	it('still records the id for the Save payload', () => {
		const harness = fakeCtx();
		const controller = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		controller.attach(harness.ctx as any, TABLE);

		controller.select(0);
		controller.deleteSelected();

		// The canvas half must not replace the persistence half — the row still has to be deleted
		// server-side on Save, not merely hidden locally.
		expect(controller.rows.map((r) => r.id)).not.toContain('a1');
	});
});
