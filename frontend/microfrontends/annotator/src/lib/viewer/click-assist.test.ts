/**
 * Click-to-segment is REACHABLE — the commit policy moved out of the tool.
 *
 * The rect tool refused any commit under 5×5 px while the backend explicitly grows a zero-size
 * region into a click patch and the assist bar's copy promised "click or drag". The tool now
 * commits everything; the CONTROLLER decides: an armed producer honours the click as a region
 * prompt, an unarmed canvas discards the accidental click exactly as before.
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
const assistCalls: unknown[] = [];
vi.mock('./remote/assist.remote', () => ({
	requestAssist: async (args: unknown) => {
		assistCalls.push(args);
		return { ok: true, data: { shapes: [], source: 'model:sam-click', dropped: [] } };
	},
	assistProducers: async () => [],
}));
vi.mock('./remote/jobs.remote', () => ({
	submitJob: async () => ({ status: 'unsupported' }),
	jobStatus: async () => null,
}));
// assist() dynamically imports the review selection (which reaches for `$app/paths`) to read the
// task id — mocked so the plain-node test can cross that seam.
vi.mock('$lib/labeling/review-selection.svelte', () => ({ reviewSelection: { taskId: null } }));

import { AnnotatorController } from './annotator.svelte.js';
import type { CommitShape } from '@rask/engine';

function harness() {
	const hidden = new Set<number>();
	let commit: ((shape: CommitShape) => void) | undefined;
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
				set onCommit(fn: (shape: CommitShape) => void) {
					commit = fn;
				},
				set onChange(_fn: unknown) {},
				set onSelect(_fn: unknown) {},
				set onDirtyChange(_fn: unknown) {},
				set onCvToolReady(_fn: unknown) {},
				set onMagneticSnap(_fn: unknown) {},
			},
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
	return { hidden, ctx, commit: (shape: CommitShape) => commit?.(shape) };
}

const TABLE = tableFromArrays({
	id: ['a'],
	shape_type: ['bbox'],
	label: ['stamp'],
	status: ['accepted'],
	source: ['human'],
});

const CLICK: CommitShape = { type: 'rect', x: 120, y: 80, width: 0, height: 0, polygon: [] };

describe('click policy at the commit seam', () => {
	it('an ARMED producer honours a click as a zero-box region prompt', async () => {
		const h = harness();
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(h.ctx as any, TABLE, '/annotator/api/annotations/d/0/1');
		c.setAssistProducer('sam-click');

		h.commit(CLICK);
		// The prompt rides an async command — drain the microtask queue before asserting.
		await vi.waitFor(() => expect(assistCalls).toHaveLength(1));
		expect(assistCalls[0]).toMatchObject({
			producer: 'sam-click',
			region: { x: 120, y: 80, width: 0, height: 0 },
		});
		// And nothing was inserted as a manual annotation.
		expect(c.pendingInserts).toBe(0);
	});

	it('an UNARMED canvas still discards the accidental click', () => {
		const h = harness();
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(h.ctx as any, TABLE);

		h.commit(CLICK);
		h.commit({ type: 'rect', x: 0, y: 0, width: 4, height: 4, polygon: [] });

		expect(c.pendingInserts).toBe(0);
	});

	it('a real manual rect still inserts', () => {
		const h = harness();
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(h.ctx as any, TABLE);

		h.commit({ type: 'rect', x: 10, y: 10, width: 40, height: 30, polygon: [] });

		expect(c.pendingInserts).toBe(1);
	});
});
