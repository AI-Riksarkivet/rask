/**
 * The interactive point-prompt session — clicks ACCUMULATE, predictions REPLACE.
 *
 * A SAM-style refinement is a conversation about ONE object: each click adds a signed point,
 * the producer re-runs over the full set, and its answer must replace the previous prediction —
 * stacking them would leave N ghost masks of the same object in the review queue, one per click.
 * These tests pin the loop end-to-end at the controller seam: the request carries the whole
 * session, exactly one prediction survives refinement, the sign toggle marks background clicks,
 * and Done/disarm end the session keeping the last answer.
 */

import { tableFromArrays } from 'apache-arrow';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('svelte-sonner', () => ({
	toast: {
		success: () => {},
		error: () => {},
		message: () => {},
		warning: () => {},
		info: () => {},
	},
}));

const assistCalls: { points?: { x: number; y: number; positive: boolean }[] | null }[] = [];
let nextConfidence = 0.75;
vi.mock('./remote/assist.remote', () => ({
	requestAssist: async (args: {
		points?: { x: number; y: number; positive: boolean }[] | null;
	}) => {
		assistCalls.push(args);
		// One polygon per call, confidence growing like the backend mock's — so each refinement
		// visibly changes the row it replaces.
		nextConfidence += 0.05;
		return {
			ok: true,
			data: {
				shapes: [
					{
						shape_type: 'polygon',
						x: 40,
						y: 40,
						width: 120,
						height: 120,
						polygon: [100, 40, 160, 100, 100, 160, 40, 100],
						label: 'object',
						confidence: nextConfidence,
						uncertainty: 0.2,
					},
				],
				source: 'model:sam-click',
				dropped: [],
			},
		};
	},
	assistProducers: async () => [],
}));
vi.mock('./remote/jobs.remote', () => ({
	submitJob: async () => ({ status: 'unsupported' }),
	jobStatus: async () => null,
}));
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

const clickAt = (x: number, y: number): CommitShape => ({
	type: 'rect',
	x,
	y,
	width: 0,
	height: 0,
	polygon: [],
});

function armed() {
	const h = harness();
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
	c.attach(h.ctx as any, TABLE, '/annotator/api/annotations/d/0/1');
	c.setAssistProducer('sam-click');
	return { h, c };
}

describe('the point-refinement session', () => {
	beforeEach(() => {
		assistCalls.length = 0;
		nextConfidence = 0.75;
	});

	it('clicks accumulate — the second request carries BOTH points', async () => {
		const { h, c } = armed();

		h.commit(clickAt(100, 100));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(1));
		h.commit(clickAt(200, 150));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(2));

		expect(assistCalls[0]?.points).toEqual([{ x: 100, y: 100, positive: true }]);
		expect(assistCalls[1]?.points).toEqual([
			{ x: 100, y: 100, positive: true },
			{ x: 200, y: 150, positive: true },
		]);
		expect(c.assistPoints).toHaveLength(2);
	});

	it('each refinement REPLACES the previous prediction — one row survives, the newest', async () => {
		const { h, c } = armed();

		h.commit(clickAt(100, 100));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(1));
		h.commit(clickAt(200, 150));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(2));

		// One insert on the wire, one prediction row visible — not two stacked ghosts.
		expect(c.pendingInserts).toBe(1);
		const predictions = c.rows.filter((r) => r.status === 'prediction');
		expect(predictions).toHaveLength(1);
	});

	it('the sign toggle marks the next click as BACKGROUND', async () => {
		const { h, c } = armed();

		h.commit(clickAt(100, 100));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(1));
		c.assistPointPositive = false;
		h.commit(clickAt(300, 300));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(2));

		expect(assistCalls[1]?.points).toEqual([
			{ x: 100, y: 100, positive: true },
			{ x: 300, y: 300, positive: false },
		]);
	});

	it('Done ends the session KEEPING the prediction; the next click starts fresh', async () => {
		const { h, c } = armed();

		h.commit(clickAt(100, 100));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(1));
		c.finishAssistSession();

		expect(c.assistPoints).toHaveLength(0);
		expect(c.pendingInserts).toBe(1); // the segmented object stays

		h.commit(clickAt(500, 500));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(2));
		// A fresh session: one point, and the finished object's row is NOT replaced.
		expect(assistCalls[1]?.points).toEqual([{ x: 500, y: 500, positive: true }]);
		await vi.waitFor(() => expect(c.pendingInserts).toBe(2));
	});

	it('a refinement swaps in a NEW row — the previous prediction id is gone', async () => {
		const { h, c } = armed();

		h.commit(clickAt(100, 100));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(1));
		expect(c.pendingInserts).toBe(1);

		const before = c.rows.find((r) => r.status === 'prediction')?.id;
		h.commit(clickAt(200, 200));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(2));
		const after = c.rows.filter((r) => r.status === 'prediction');
		expect(after).toHaveLength(1);
		expect(after[0]?.id).not.toBe(before);
	});

	it('disarming ends the session and keeps the last answer', async () => {
		const { h, c } = armed();

		h.commit(clickAt(100, 100));
		await vi.waitFor(() => expect(assistCalls).toHaveLength(1));
		c.setAssistProducer(null);

		expect(c.assistPoints).toHaveLength(0);
		expect(c.pendingInserts).toBe(1);
	});
});
