/**
 * Draft links load BACK — the reopen-wipe fix.
 *
 * Links ride the task draft beside the shapes, and the draft sync posts `controller.links` on
 * every save. Before `restoreLinks`, nothing ever read them back in: a reopened task started at
 * `links = []`, so its first save destroyed every previously drawn relation. The suite pins the
 * restore (into state AND onto the canvas) and the no-clobber rule that protects links drawn
 * while the async draft read was still in flight.
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
	const pushedLinks: unknown[][] = [];
	const ctx = {
		plugins: {
			arrow: {
				setLinks: (edges: unknown[]) => void pushedLinks.push(edges),
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
	return { hidden, pushedLinks, ctx };
}

const TABLE = tableFromArrays({
	id: ['q1', 'v1'],
	shape_type: ['bbox', 'bbox'],
	label: ['question', 'value'],
	status: ['accepted', 'accepted'],
	source: ['human', 'human'],
});

const LINK = { name: 'answers', from_shape: 'q1', to_shape: 'v1' };

describe('restoreLinks', () => {
	it('restores the draft snapshot into state AND onto the canvas', () => {
		const h = harness();
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(h.ctx as any, TABLE);

		c.restoreLinks([LINK]);

		expect(c.links).toEqual([LINK]);
		expect(c.linksFor('q1')).toHaveLength(1);
		// The canvas got the edge as an id→index pair (0 → 1 in this table).
		expect(h.pushedLinks.at(-1)).toEqual([{ from: 0, to: 1 }]);
	});

	it('declines to clobber links drawn while the draft read was in flight', () => {
		const h = harness();
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(h.ctx as any, TABLE);
		c.addLink({ name: 'annotates', from_shape: 'v1', to_shape: 'q1' });

		c.restoreLinks([LINK]);

		// The newer, human-drawn link wins; the stale snapshot does not replace it.
		expect(c.links).toEqual([{ name: 'annotates', from_shape: 'v1', to_shape: 'q1' }]);
	});

	it('a restored link keeps riding the draft — the next sync posts it, not []', () => {
		const h = harness();
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(h.ctx as any, TABLE);

		c.restoreLinks([LINK]);

		// `links` is exactly what draft-sync snapshots (syncTaskDraft(taskId, table, links)).
		expect(c.links).toEqual([LINK]);
		// And a link drawn ON TOP composes rather than replaces.
		c.addLink({ name: 'annotates', from_shape: 'v1', to_shape: 'q1' });
		expect(c.links).toHaveLength(2);
	});
});
