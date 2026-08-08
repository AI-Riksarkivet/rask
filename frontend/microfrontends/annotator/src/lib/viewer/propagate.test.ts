/**
 * Few-shot propagation reaches the jobs seam — the flow the batch plane was designed around,
 * previously callable and uncalled. The suite pins what the PANEL depends on: `unitKey` parses
 * the open unit out of the annotations URL, and `propagate()` resolves exemplar INDICES to
 * stable ids and posts a `propagate` job over the chosen chunk-level scope.
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
vi.mock('./remote/assist.remote', () => ({
	requestAssist: async () => ({ ok: true, data: { shapes: [], source: 's', dropped: [] } }),
	assistProducers: async () => [],
}));
const jobs: unknown[] = [];
vi.mock('./remote/jobs.remote', () => ({
	submitBatchJob: async (args: unknown) => {
		jobs.push(args);
		return { ok: true, data: { job_id: 'job-abc', status: 'queued', backend: 'mock' } };
	},
	jobStatus: async () => null,
}));

import { AnnotatorController } from './annotator.svelte.js';

function harness() {
	const noop = () => {};
	const ctx = {
		plugins: {
			arrow: new Proxy(
				{ getDirtyOverrides: () => new Map(), isDeleted: () => false },
				{ get: (t: Record<string, unknown>, k: string) => t[k] ?? noop },
			),
			interaction: new Proxy(
				{ getSelectedSet: () => new Set<number>(), cvCapable: false },
				{ get: (t: Record<string, unknown>, k: string) => t[k] ?? noop, set: () => true },
			),
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
	return ctx;
}

const TABLE = tableFromArrays({
	id: ['ex-1', 'ex-2', 'other'],
	shape_type: ['mask', 'mask', 'bbox'],
	label: ['stamp', 'stamp', 'line'],
	status: ['accepted', 'accepted', 'accepted'],
	source: ['human', 'human', 'human'],
});

describe('few-shot propagation', () => {
	beforeEach(() => {
		jobs.length = 0;
	});

	it('unitKey parses the open unit out of the annotations URL', () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE, '/annotator/api/annotations/doc9/0/19?dataset=demo');

		expect(c.unitKey).toBe('doc9/0/19');
	});

	it('an unattached canvas has no unit key rather than a wrong one', () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE);

		expect(c.unitKey).toBeNull();
	});

	it('MANY exemplars propagate over MANY items in one job — keys is plural on purpose', async () => {
		// The INSID3 shape: a few normally-labelled objects on ONE page become the instruction;
		// the deriver applies the pattern across every unit the scope names. Neither side is
		// singular — two exemplar masks here, three items there, one job.
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE, '/annotator/api/annotations/doc9/0/19');

		const outcome = c.propagate(
			{ level: 'chunks', keys: ['doc9/0/19', 'doc9/0/20', 'doc9/0/21'] },
			[0, 1],
		);

		expect(outcome.status).toBe('queued');
		await vi.waitFor(() => expect(jobs.length).toBeGreaterThan(0));
		expect(jobs.at(-1)).toMatchObject({
			producer: 'insid3',
			op: 'propagate',
			scope: { level: 'chunks', keys: ['doc9/0/19', 'doc9/0/20', 'doc9/0/21'] },
			exemplars: ['ex-1', 'ex-2'],
		});
	});

	it('propagate() posts ONE job: exemplar indices resolved to ids, over the given scope', async () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE, '/annotator/api/annotations/doc9/0/19');

		const outcome = c.propagate({ level: 'chunks', keys: ['doc9/0/19'] }, [0, 1]);

		expect(outcome.status).toBe('queued');
		await vi.waitFor(() => expect(jobs).toHaveLength(1));
		expect(jobs[0]).toMatchObject({
			producer: 'insid3',
			op: 'propagate',
			scope: { level: 'chunks', keys: ['doc9/0/19'] },
			// Ids, never indices: rows renumber on reload, and the deriver fetches masks by id.
			exemplars: ['ex-1', 'ex-2'],
		});
	});
});
