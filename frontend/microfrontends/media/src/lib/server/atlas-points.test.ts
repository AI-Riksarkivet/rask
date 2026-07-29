import type { RequestEvent } from '@sveltejs/kit';
import { describe, expect, it, vi } from 'vitest';
import { makeAtlasPointsRoute, resourceKey } from './atlas-points';

/**
 * The route these tests guard is a SHARED cache in front of a 6.6 MB read that, before it existed, an
 * anonymous socket could take in full through the ingress (measured: `200 6678928`, no session, no
 * bearer). So the assertions that matter are the ones a happy-path suite cannot see: that the
 * authorization runs on every single call including a warm one, that a refused caller gets nothing even
 * when the entry is hot, and that ten concurrent cold hits are one upstream read rather than ten.
 */

const ENV = { VIEWER_API: 'http://viewer:8101' };
const POINTS = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]);
const URL_TEXT = 'https://estate.example.com/media/api/atlas/points?space=text&v=6&dataset=demo';

/** A stub viewer that records every call and can be told how to answer the authorization probe. */
function viewer(opts: { probeStatus?: number; pointsStatus?: number; delayMs?: number } = {}) {
	const calls: string[] = [];
	const fetch = vi.fn(async (input: string | URL | Request) => {
		const url = String(input);
		calls.push(url);
		if (url.includes('/atlas/status')) {
			const status = opts.probeStatus ?? 200;
			return new Response(status === 200 ? '{"projected":true}' : '{"detail":"forbidden"}', {
				status,
				headers: { 'content-type': 'application/json' },
			});
		}
		if (opts.delayMs) await new Promise((r) => setTimeout(r, opts.delayMs));
		return new Response(POINTS, {
			status: opts.pointsStatus ?? 200,
			headers: {
				'content-type': 'application/vnd.apache.arrow.stream',
				'cache-control': 'public, max-age=300',
			},
		});
	});
	const of = (fragment: string) => calls.filter((c) => c.includes(fragment));
	return { fetch, calls, probes: () => of('/atlas/status'), points: () => of('/atlas/points') };
}

/** A minimal RequestEvent double — the handler reads only url, fetch and locals. */
function event(
	fetch: ReturnType<typeof viewer>['fetch'],
	locals: { authEnabled: boolean; signedInAs?: string },
	href = URL_TEXT,
): RequestEvent {
	return {
		url: new URL(href),
		fetch,
		locals: {
			authEnabled: locals.authEnabled,
			session: locals.signedInAs
				? {
						sub: locals.signedInAs,
						name: locals.signedInAs,
						email: null,
						accessToken: `AT-${locals.signedInAs}`,
						expiresAt: 0,
					}
				: null,
		},
	} as unknown as RequestEvent;
}

const ALICE = { authEnabled: true, signedInAs: 'alice' };
const BOB = { authEnabled: true, signedInAs: 'bob' };
const ANON = { authEnabled: true };

const call = (route: ReturnType<typeof makeAtlasPointsRoute>, ev: RequestEvent) =>
	Promise.resolve(route.GET(ev)) as Promise<Response>;

describe('the atlas-points route authorizes every request', () => {
	it('refuses an anonymous caller EVEN when the entry is warm, and serves no bytes', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();

		// alice warms it. This is the state that makes the naive implementation leak.
		const warm = await call(route, event(up.fetch, ALICE));
		expect(warm.status).toBe(200);
		expect(route.cache.stats().entries).toBe(1);

		const anon = await call(route, event(up.fetch, ANON));

		expect(anon.status).toBe(401);
		expect(await anon.json()).toEqual({ detail: 'sign in required' });
		expect(anon.headers.get('x-cache')).toBeNull();
		// …and the refusal cost the estate nothing upstream: no probe, no second read.
		expect(up.probes()).toHaveLength(1);
		expect(up.points()).toHaveLength(1);
	});

	it('refuses a caller the RESOURCE refuses, even when the entry is warm', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const allowed = viewer();
		await call(route, event(allowed.fetch, ALICE)); // warm
		expect(route.cache.stats().entries).toBe(1);

		// The same resource, a caller the viewer will not authorize (what #103's FGA check will look like).
		const denied = viewer({ probeStatus: 403 });
		const res = await call(route, event(denied.fetch, BOB));

		expect(res.status).toBe(403);
		expect(res.headers.get('x-cache')).toBeNull();
		expect(new Uint8Array(await res.arrayBuffer())).not.toEqual(POINTS);
		expect(denied.points()).toHaveLength(0); // never even asked for the payload
	});

	it('probes the resource on EVERY call, including cache hits', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();

		for (let i = 0; i < 4; i++) await call(route, event(up.fetch, ALICE));

		expect(up.probes()).toHaveLength(4); // the decision is never cached
		expect(up.points()).toHaveLength(1); // only the payload is
	});

	it('fails closed when the authorizing upstream is unreachable, warm entry or not', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();
		await call(route, event(up.fetch, ALICE));

		const dead = vi.fn(async (input: string | URL | Request) => {
			if (String(input).includes('/atlas/status')) throw new Error('ECONNREFUSED');
			return new Response(POINTS);
		});
		const res = await call(route, event(dead as never, ALICE));

		expect(res.status).toBe(502);
		expect(res.headers.get('x-cache')).toBeNull();
	});

	it('a stack with auth off is unchanged — no session to require', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();

		const res = await call(route, event(up.fetch, { authEnabled: false }));

		expect(res.status).toBe(200);
		expect(res.headers.get('x-cache')).toBe('miss');
	});
});

describe('the atlas-points route keys on the resource, not the caller', () => {
	it("serves bob from alice's entry — one fill for everyone allowed to see it", async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();

		const alice = await call(route, event(up.fetch, ALICE));
		const bob = await call(route, event(up.fetch, BOB));

		expect(alice.headers.get('x-cache')).toBe('miss');
		expect(bob.headers.get('x-cache')).toBe('hit'); // bob's FIRST load, already warm
		expect(new Uint8Array(await bob.arrayBuffer())).toEqual(POINTS);
		expect(up.points()).toHaveLength(1);
		// The bearer differs per caller and must never reach the key.
		expect(resourceKey(new URL(URL_TEXT))).toBe('atlas/points?dataset=demo&space=text');
	});

	it('canonicalises the key so parameter order cannot fork the cache', () => {
		const a = 'https://e/media/api/atlas/points?space=text&v=6&dataset=demo';
		const b = 'https://e/media/api/atlas/points?dataset=demo&v=6&space=text';
		expect(resourceKey(new URL(a))).toBe(resourceKey(new URL(b)));
	});

	it('drops parameters that are not the resource — from the key AND from the upstream URL', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();

		await call(route, event(up.fetch, ALICE, `${URL_TEXT}&cachebust=${Date.now()}&user=bob`));

		expect(route.cache.stats().entries).toBe(1);
		expect(up.points()[0]).toBe('http://viewer:8101/api/atlas/points?dataset=demo&space=text');
		// A second caller with different junk gets the same entry rather than a second 6.6 MB read.
		await call(route, event(up.fetch, BOB, `${URL_TEXT}&cachebust=other`));
		expect(up.points()).toHaveLength(1);
	});

	it('a caller-supplied `v` can NOT fork the cache — the amplification hole', async () => {
		// Measured against the live estate before this was closed: six junk `v` tokens evicted the product
		// entry (`x-cache: hit` -> `miss`), and twenty tokens from ONE signed-in session added twenty reads
		// to the viewer's access log — 133 MB on demand, the shared entry permanently cold, and
		// single-flight defeated because every key was unique. That is the same burst of concurrent
		// multi-megabyte reads that OOM-killed the viewer.
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();

		await call(route, event(up.fetch, ALICE, URL_TEXT)); // the product URL, v=6
		for (const junk of ['1', '99', 'x', '', '6.0', String(Date.now())]) {
			await call(route, event(up.fetch, BOB, `${URL_TEXT.replace('v=6', `v=${junk}`)}`));
		}

		// One resource, one fill, one entry — no matter what any caller puts in `v`.
		expect(up.points()).toHaveLength(1);
		expect(route.cache.stats().entries).toBe(1);
	});

	it('a different space is a different resource', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer();

		await call(route, event(up.fetch, ALICE));
		await call(route, event(up.fetch, ALICE, URL_TEXT.replace('space=text', 'space=visual')));

		expect(up.points()).toHaveLength(2);
		expect(route.cache.stats().entries).toBe(2);
	});
});

describe('the atlas-points route single-flights the expensive read', () => {
	it('ten concurrent cold hits are ONE upstream fetch — the case that OOM-killed the viewer', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer({ delayMs: 15 });

		const responses = await Promise.all(
			Array.from({ length: 10 }, () => call(route, event(up.fetch, ALICE))),
		);

		expect(up.points()).toHaveLength(1);
		expect(up.probes()).toHaveLength(10); // ten authorizations, one payload
		expect(responses.filter((r) => r.headers.get('x-cache') === 'shared')).toHaveLength(9);
		for (const r of responses) expect(new Uint8Array(await r.arrayBuffer())).toEqual(POINTS);
	});

	it('an unreachable viewer is a 502 with a parseable body, not the zone error page', async () => {
		const route = makeAtlasPointsRoute(ENV);
		// Authorization succeeds, then the payload read fails — a rejection here would reach the client as
		// SvelteKit's generic 500 HTML, which the browser client cannot parse into an ApiError.
		const flaky = vi.fn(async (input: string | URL | Request) => {
			if (String(input).includes('/atlas/status')) {
				return new Response('{"projected":true}', { status: 200 });
			}
			throw new Error('ECONNRESET talking to the viewer');
		});
		const res = await call(route, event(flaky as never, ALICE));

		expect(res.status).toBe(502);
		expect(await res.json()).toMatchObject({ error: expect.stringContaining('ECONNRESET') });
		expect(route.cache.stats().entries).toBe(0);
	});

	it('an upstream failure is not retained', async () => {
		const route = makeAtlasPointsRoute(ENV);
		const up = viewer({ pointsStatus: 503 });

		const res = await call(route, event(up.fetch, ALICE));

		expect(res.status).toBe(503);
		expect(route.cache.stats().entries).toBe(0);
	});
});
