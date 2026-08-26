import { describe, expect, it, vi } from 'vitest';
import { ingestLifecycle } from '../src/ingest';

/**
 * The lifecycle doors' client.
 *
 * These three routes shipped with the Dapr audit drain and had ZERO callers in any zone — reachable
 * only by curl. Wiring them to a page made this client the seam where a REFUSAL has to survive as a
 * value: 409 means "the run is not in a state this verb applies to" and 503 means "the engine did not
 * answer", and an operator needs to be able to tell those apart. Thrown, they collapse into one red
 * toast; returned, the page can say which.
 */

function res(status: number, body: unknown, ok = status < 400): Response {
	return {
		ok,
		status,
		statusText: 'x',
		json: async () => body,
	} as unknown as Response;
}

describe('ingestLifecycle', () => {
	it('posts to the verb the caller asked for, under the gateway ingest row', async () => {
		const fetchFn = vi.fn(async () =>
			res(202, { run_id: 'r1', state: 'SUSPENDED', detail: 'held' }),
		);

		await ingestLifecycle('r1', 'pause', fetchFn as unknown as typeof fetch);

		// `/api/ingest` is the gateway row; the service's own RASK_API_PREFIX is `/api`, so the path
		// the browser sends is `/api/ingest/ingests/...`. A wrong prefix here is a silent 404.
		expect(fetchFn).toHaveBeenCalledWith(
			'/api/ingest/ingests/r1/pause',
			expect.objectContaining({ method: 'POST' }),
		);
	});

	it('encodes the run id, so a slash in an id cannot forge a path segment', async () => {
		const fetchFn = vi.fn(async () => res(202, { run_id: 'a/b' }));

		await ingestLifecycle('a/b', 'terminate', fetchFn as unknown as typeof fetch);

		expect(fetchFn.mock.calls[0]?.[0]).toBe('/api/ingest/ingests/a%2Fb/terminate');
	});

	it('carries the caller bearer — a governed door refuses the gateway itself', async () => {
		const fetchFn = vi.fn(async () => res(202, { run_id: 'r1' }));

		await ingestLifecycle('r1', 'terminate', fetchFn as unknown as typeof fetch, {
			authorization: 'Bearer tok',
		});

		expect(fetchFn.mock.calls[0]?.[1]).toMatchObject({ headers: { authorization: 'Bearer tok' } });
	});

	it('returns a 409 as a VALUE carrying the door’s reason, never a throw', async () => {
		const fetchFn = vi.fn(async () =>
			res(409, { detail: "ingest run 'r1' is COMPLETED, not running — nothing to terminate" }),
		);

		const out = await ingestLifecycle('r1', 'terminate', fetchFn as unknown as typeof fetch);

		expect(out.ok).toBe(false);
		if (!out.ok) {
			expect(out.status).toBe(409);
			// The DOOR's words, not ours — it names the state the run is actually in.
			expect(out.detail).toContain('COMPLETED');
		}
	});

	it('keeps the status line when the error body is not JSON', async () => {
		const fetchFn = vi.fn(async () => ({
			ok: false,
			status: 503,
			statusText: 'Service Unavailable',
			json: async () => {
				throw new Error('not json');
			},
		}));

		const out = await ingestLifecycle('r1', 'resume', fetchFn as unknown as typeof fetch);

		expect(out.ok).toBe(false);
		if (!out.ok) expect(out.detail).toContain('503');
	});

	it('parses the accepted body, so `detail` survives to the page unaltered', async () => {
		const detail =
			'further scheduling stops; an activity already in flight runs to completion, and the run holds its queue and consumer until resumed';
		const fetchFn = vi.fn(async () => res(202, { run_id: 'r1', state: 'SUSPENDED', detail }));

		const out = await ingestLifecycle('r1', 'pause', fetchFn as unknown as typeof fetch);

		expect(out.ok).toBe(true);
		// Load-bearing: the 202 bodies were written to stop the misreading a button invites, so a
		// client that dropped or rewrote `detail` would undo the reason they exist.
		if (out.ok) expect(out.value.detail).toBe(detail);
	});
});
