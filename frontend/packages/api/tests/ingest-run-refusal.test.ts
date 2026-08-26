import { describe, expect, it, vi } from 'vitest';
import { getIngestRun } from '../src/ingest';

/**
 * A REFUSAL AND AN ABSENCE ARE DIFFERENT FACTS, and the run-detail page could not tell them apart.
 *
 * Measured on the live estate 2026-08-26. A run was started in project `demo`; the signed-in user
 * held only `namespace:silver#writer`, so the read door answered 403. The page rendered:
 *
 *     "No such run. The ingest plane has no record of <id>. Neither its accepted record nor a
 *      workflow for it exists — a run that had merely lost its progress would still answer here."
 *
 * Every clause of that was false. The record existed (the service returned it to a service-token
 * caller in the same minute) and so did the workflow (35 rows in the Dapr state store). The page
 * told an operator their run had vanished when the truth was that they lacked a grant — which sends
 * them to look for a lost run instead of to an admin.
 *
 * The root cause is here, not in the page: `refuse()` threw `new Error("getIngestRun: <detail>")`
 * and DISCARDED the status, so no caller downstream could branch on it however much it wanted to.
 */

function res(status: number, body: unknown): Response {
	return {
		ok: false,
		status,
		statusText: 'x',
		json: async () => body,
	} as unknown as Response;
}

describe('getIngestRun refusal', () => {
	it('preserves the STATUS on a 403, so a denial is not reported as an absence', async () => {
		const fetchFn = vi.fn(async () =>
			res(403, { detail: 'user:alice@example.com lacks admin on project:demo' }),
		);

		const err = await getIngestRun('r1', fetchFn as unknown as typeof fetch).then(
			() => null,
			(e: unknown) => e,
		);

		expect(err).toBeInstanceOf(Error);
		expect((err as { status?: number }).status).toBe(403);
	});

	it('preserves the STATUS on a 404, which is the only case that IS an absence', async () => {
		const fetchFn = vi.fn(async () => res(404, { detail: 'no such run' }));

		const err = await getIngestRun('r1', fetchFn as unknown as typeof fetch).then(
			() => null,
			(e: unknown) => e,
		);

		expect((err as { status?: number }).status).toBe(404);
	});

	it('preserves the STATUS on a 503, so an unreachable engine is not read as a missing run', async () => {
		const fetchFn = vi.fn(async () => res(503, { detail: 'workflow engine unreachable' }));

		const err = await getIngestRun('r1', fetchFn as unknown as typeof fetch).then(
			() => null,
			(e: unknown) => e,
		);

		expect((err as { status?: number }).status).toBe(503);
	});

	it('still carries the door’s own words in the message, which is what the page shows', async () => {
		const fetchFn = vi.fn(async () =>
			res(403, { detail: 'user:alice@example.com lacks admin on project:demo' }),
		);

		const err = await getIngestRun('r1', fetchFn as unknown as typeof fetch).then(
			() => null,
			(e: unknown) => e,
		);

		expect((err as Error).message).toContain('lacks admin on project:demo');
	});
});
