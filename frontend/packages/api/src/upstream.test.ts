/**
 * The `ApiResult` producer's contract (#93).
 *
 * Sixteen hand-rolled copies of this logic drifted on exactly the cases below — nine said
 * unreachable = 0, four said 502, and the non-JSON-200 guard existed in one copy out of sixteen.
 * These pin the ONE implementation so the drift class cannot re-open: every consumer's
 * honest-state branch keys on these statuses, so a change here is a wire-visible change to all
 * seven zones at once.
 */

import { describe, expect, it } from 'vitest';
import * as v from 'valibot';

import { failText, parsed, upstreamJSON } from './upstream';

const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const request = (fetchImpl: typeof globalThis.fetch, init?: RequestInit) =>
	upstreamJSON({ fetch: fetchImpl, base: 'http://up', path: '/x', init, upstream: 'catalog' });

describe('the three outcomes', () => {
	it('a rejected fetch is status 0 — UNREACHABLE, never a throw', async () => {
		// A rejection crossing the remote boundary skips every caller's offline branch; nine of the
		// sixteen copies got this right and four returned 502, which is why models rendered an
		// "unreachable" state its own transport could not produce.
		const result = await request(() => Promise.reject(new Error('ECONNREFUSED')));

		expect(result).toEqual({ ok: false, status: 0, detail: 'Error: ECONNREFUSED' });
	});

	it("a non-2xx carries the upstream's own {detail} when it sent one", async () => {
		const result = await request(() => Promise.resolve(json({ detail: 'no such table' }, 404)));

		expect(result).toEqual({ ok: false, status: 404, detail: 'no such table' });
	});

	it('a non-2xx without a JSON body names the upstream and the status', async () => {
		// "answered 503" alone cannot say WHICH upstream — the name is what stops a reader debugging
		// the wrong service.
		const result = await request(() => Promise.resolve(new Response('gateway timeout', { status: 503 })));

		expect(result).toEqual({ ok: false, status: 503, detail: 'catalog answered 503' });
	});

	it('a 2xx JSON body is the success value', async () => {
		const result = await request(() => Promise.resolve(json({ rows: [1] })));

		expect(result).toEqual({ ok: true, data: { rows: [1] } });
	});

	it('a 200 with a NON-JSON body is 502 contract drift, not success and not a throw', async () => {
		// The guard exactly ONE of the sixteen copies had. Without it, res.json()'s rejection escapes
		// the shared helper into the remote boundary its own contract forbids crossing.
		const result = await request(() => Promise.resolve(new Response('<html>', { status: 200 })));

		expect(result.ok).toBe(false);
		if (!result.ok) {
			expect(result.status).toBe(502);
			expect(result.detail).toContain('non-JSON');
		}
	});
});

describe('headers', () => {
	it('carries the bearer when given one, and no authorization header otherwise', async () => {
		let seen: Headers | undefined;
		const capture: typeof globalThis.fetch = (_, init) => {
			seen = new Headers(init?.headers);
			return Promise.resolve(json({}));
		};

		await upstreamJSON({ fetch: capture, base: 'http://up', path: '/x', bearer: 'tok', upstream: 'u' });
		expect(seen?.get('authorization')).toBe('Bearer tok');

		await upstreamJSON({ fetch: capture, base: 'http://up', path: '/x', upstream: 'u' });
		expect(seen?.get('authorization')).toBeNull();
	});

	it('sets content-type ONLY when a body rides along, and caller headers win', async () => {
		let seen: Headers | undefined;
		const capture: typeof globalThis.fetch = (_, init) => {
			seen = new Headers(init?.headers);
			return Promise.resolve(json({}));
		};

		await upstreamJSON({ fetch: capture, base: 'http://u', path: '/x', upstream: 'u' });
		expect(seen?.get('content-type')).toBeNull();

		await upstreamJSON({
			fetch: capture,
			base: 'http://u',
			path: '/x',
			init: { method: 'POST', body: '{}', headers: { 'content-type': 'application/x-ndjson' } },
			upstream: 'u',
		});
		expect(seen?.get('content-type')).toBe('application/x-ndjson');
	});
});

describe('parsed', () => {
	const Row = v.object({ id: v.string() });

	it('passes a failure through UNTOUCHED — parsing a failure would invent a second error', () => {
		const fail = { ok: false as const, status: 404, detail: 'gone' };

		expect(parsed(fail, Row)).toBe(fail);
	});

	it('parses a success at the boundary', () => {
		expect(parsed({ ok: true, data: { id: 'a' } }, Row)).toEqual({ ok: true, data: { id: 'a' } });
	});

	it('a schema miss is 502 contract drift naming the upstream, never a cast', () => {
		const result = parsed({ ok: true, data: { id: 7 } }, Row, 'catalog');

		expect(result.ok).toBe(false);
		if (!result.ok) {
			expect(result.status).toBe(502);
			expect(result.detail).toContain('catalog contract drift');
		}
	});
});

describe('failText — the 26-file ladder, written once', () => {
	it('401 asks for sign-in, naming the action', () => {
		expect(failText({ status: 401, detail: 'x' }, 'load the queue')).toBe('Sign in to load the queue.');
	});

	it('403 names the missing permission target, not just "denied"', () => {
		expect(failText({ status: 403, detail: 'x' }, 'publish')).toContain('permission to publish');
	});

	it('0 says unreachable and keeps the transport detail for the curious', () => {
		const text = failText({ status: 0, detail: 'ECONNREFUSED' }, 'list tables');

		expect(text).toContain('unreachable');
		expect(text).toContain('ECONNREFUSED');
	});

	it("anything else is the upstream's own words — they are the actionable half", () => {
		expect(failText({ status: 409, detail: 'name already taken' }, 'create')).toBe('name already taken');
	});
});
