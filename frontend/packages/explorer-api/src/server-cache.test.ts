import { describe, expect, it, vi } from 'vitest';
import { SharedPayloadCache, shareableTtlMs } from './server-cache';

/**
 * A cache in the zone server is shared between users, so the tests that matter are not the hit-rate ones.
 * The failure this file exists to catch is the one a happy-path suite is blind to: an implementation that
 * consults the entry map before it consults the authorization, and therefore hands a warm 6.6 MB
 * projection to a caller who may not read it. Every ordering assertion below is about that.
 */

const PAYLOAD = new TextEncoder().encode('a 6.6 MB arrow stream, in spirit');

/** An upstream that counts its calls and can be told what to answer. */
function upstream(
	body: Uint8Array<ArrayBuffer> | string = PAYLOAD,
	init: { status?: number; headers?: Record<string, string>; delayMs?: number } = {},
) {
	const load = vi.fn(async () => {
		if (init.delayMs) await new Promise((r) => setTimeout(r, init.delayMs));
		return new Response(body, {
			status: init.status ?? 200,
			headers: {
				'content-type': 'application/vnd.apache.arrow.stream',
				'cache-control': 'public, max-age=300',
				...init.headers,
			},
		});
	});
	return load;
}

const allow = () => Promise.resolve(null);
const refuse = (status = 403) =>
	Promise.resolve(new Response(JSON.stringify({ detail: 'not yours' }), { status }));

const KEY = 'atlas/points?dataset=demo&space=text&v=6';
const CEILING = 300_000;

describe('SharedPayloadCache — authorization', () => {
	it('refuses a caller who may not read it EVEN when the entry is warm, and leaks no bytes', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream();

		// Someone allowed warms the entry first — this is the state a naive implementation gets wrong.
		const warm = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });
		expect(warm.status).toBe(200);
		expect(cache.stats().entries).toBe(1);

		const denied = await cache.serve(KEY, { authorize: () => refuse(), load, maxAgeMs: CEILING });

		expect(denied.status).toBe(403);
		expect(await denied.text()).toBe(JSON.stringify({ detail: 'not yours' }));
		expect(denied.headers.get('x-cache')).toBeNull(); // never touched the entry at all
		expect(cache.stats().refusals).toBe(1);
		expect(load).toHaveBeenCalledTimes(1); // and the warm entry is still there for whoever may read it
	});

	it('runs the authorization on EVERY call, never once per entry', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream();
		const authorize = vi.fn(allow);

		for (let i = 0; i < 5; i++) await cache.serve(KEY, { authorize, load, maxAgeMs: CEILING });

		expect(authorize).toHaveBeenCalledTimes(5); // the decision is never cached
		expect(load).toHaveBeenCalledTimes(1); // only the payload is
	});

	it('authorizes before touching the map — a refusal on a cold key issues no upstream read either', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream();

		const res = await cache.serve(KEY, { authorize: () => refuse(401), load, maxAgeMs: CEILING });

		expect(res.status).toBe(401);
		expect(load).not.toHaveBeenCalled();
		expect(cache.stats().entries).toBe(0);
	});

	it('one entry serves a different caller — the key is the resource, not the subject', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream();

		const alice = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });
		const bob = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });

		expect(alice.headers.get('x-cache')).toBe('miss');
		expect(bob.headers.get('x-cache')).toBe('hit'); // bob's FIRST load is already warm
		expect(new Uint8Array(await bob.arrayBuffer())).toEqual(PAYLOAD);
		expect(load).toHaveBeenCalledTimes(1);
	});
});

describe('SharedPayloadCache — single flight', () => {
	it('ten cold hits trigger ONE upstream fetch', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream(PAYLOAD, { delayMs: 10 });

		const responses = await Promise.all(
			Array.from({ length: 10 }, () =>
				cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING }),
			),
		);

		// The whole reason the viewer OOM-died: ten concurrent multi-megabyte reads instead of one.
		expect(load).toHaveBeenCalledTimes(1);
		expect(cache.stats().loads).toBe(1);
		expect(
			responses.map((r) => r.headers.get('x-cache')).filter((s) => s === 'shared'),
		).toHaveLength(9);
		for (const r of responses) expect(new Uint8Array(await r.arrayBuffer())).toEqual(PAYLOAD);
	});

	it('every waiter is authorized independently — a refused one does not ride the shared fill', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream(PAYLOAD, { delayMs: 10 });

		const [ok, denied] = await Promise.all([
			cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING }),
			cache.serve(KEY, { authorize: () => refuse(), load, maxAgeMs: CEILING }),
		]);

		expect(ok.status).toBe(200);
		expect(denied.status).toBe(403);
		expect(new Uint8Array(await denied.arrayBuffer())).not.toEqual(PAYLOAD);
	});

	it('a non-2xx fill is never shared — a waiter asks again with its OWN credentials', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		// A 403 can be about the WINNER's bearer. Handing it to a waiter would refuse someone allowed.
		let call = 0;
		const load = vi.fn(async () => {
			call += 1;
			await new Promise((r) => setTimeout(r, 10));
			return call === 1
				? new Response('forbidden for that bearer', { status: 403 })
				: new Response(PAYLOAD, {
						status: 200,
						headers: { 'cache-control': 'public, max-age=300' },
					});
		});

		const [first, second] = await Promise.all([
			cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING }),
			cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING }),
		]);

		expect(first.status).toBe(403);
		expect(second.status).toBe(200);
		expect(load).toHaveBeenCalledTimes(2);
		expect(cache.stats().entries).toBe(1); // only the 200 was retained
	});

	it('a fill that dies while READING the body still releases its waiters', async () => {
		// The path that hangs if the in-flight promise is only settled on the happy returns: the upstream
		// answered 200, so `load` resolved, and the failure is in draining the body. A waiter that is
		// already parked on that promise would wait for ever — a connection held open with no error
		// anywhere to explain it, which is strictly worse than a 502.
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		let attempt = 0;
		const load = vi.fn(async () => {
			attempt += 1;
			await new Promise((r) => setTimeout(r, 10));
			if (attempt === 1) {
				return new Response(
					new ReadableStream({
						start: (c) => c.error(new Error('connection reset mid-body')),
					}),
					{ status: 200, headers: { 'cache-control': 'public, max-age=300' } },
				);
			}
			return new Response(PAYLOAD, { headers: { 'cache-control': 'public, max-age=300' } });
		});

		const winner = cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });
		const waiter = new Promise<Response>((resolve) => {
			setTimeout(() => resolve(cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING })), 1);
		});

		await expect(winner).rejects.toThrow();
		// 2s, not the suite default: if the backstop is missing this never settles, and the point is to
		// see it fail as a timeout rather than to hang the run.
		const served = await Promise.race([
			waiter,
			new Promise<string>((r) => setTimeout(() => r('WAITER HUNG'), 2000)),
		]);
		expect(served).not.toBe('WAITER HUNG');
		expect((served as Response).status).toBe(200);
	});

	it('a thrown fill is not remembered — a failed load stays retryable', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		let attempt = 0;
		const load = vi.fn(async () => {
			attempt += 1;
			if (attempt === 1) throw new Error('ECONNREFUSED viewer');
			return new Response(PAYLOAD, { headers: { 'cache-control': 'public, max-age=300' } });
		});

		await expect(cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING })).rejects.toThrow(
			'ECONNREFUSED viewer',
		);
		expect(cache.stats().entries).toBe(0);
		const recovered = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });
		expect(recovered.status).toBe(200);
	});
});

describe('SharedPayloadCache — what a shared cache may keep', () => {
	it.each([
		['no-store', 'no-store'],
		['private', 'private, max-age=300'],
		['no-cache', 'no-cache'],
		['max-age=0', 'public, max-age=0'],
	])('does not retain a %s response', async (_label, cc) => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream(PAYLOAD, { headers: { 'cache-control': cc } });

		const res = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });

		expect(res.status).toBe(200); // the caller is still served
		expect(res.headers.get('x-cache')).toBe('bypass');
		expect(cache.stats().entries).toBe(0);
		await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });
		expect(load).toHaveBeenCalledTimes(2); // …but nothing was kept for the next caller
	});

	it('does not retain a body that varies by WHO asked', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream(PAYLOAD, { headers: { vary: 'Authorization' } });

		const res = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });

		expect(res.headers.get('x-cache')).toBe('bypass');
		expect(cache.stats().entries).toBe(0);
	});

	it('takes the STRICTER of the route ceiling and the upstream max-age', () => {
		const short = new Response('', { headers: { 'cache-control': 'public, max-age=30' } });
		expect(shareableTtlMs(short, 300_000)).toBe(30_000);

		const long = new Response('', { headers: { 'cache-control': 'public, max-age=86400' } });
		expect(shareableTtlMs(long, 300_000)).toBe(300_000);

		// s-maxage is the shared-cache directive and outranks max-age.
		const shared = new Response('', {
			headers: { 'cache-control': 'public, max-age=600, s-maxage=60' },
		});
		expect(shareableTtlMs(shared, 300_000)).toBe(60_000);

		// No Cache-Control at all: the route's claim stands (it is the one that knows the key is versioned).
		expect(shareableTtlMs(new Response(''), 300_000)).toBe(300_000);
	});

	it('serves nothing past its expiry, even with nothing sweeping it', async () => {
		let clock = 1_000_000;
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20, now: () => clock });
		const load = upstream(PAYLOAD, { headers: { 'cache-control': 'public, max-age=300' } });

		await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });
		clock += 299_000;
		expect(
			(await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING })).headers.get(
				'x-cache',
			),
		).toBe('hit');
		clock += 2_000; // now past max-age=300
		expect(
			(await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING })).headers.get(
				'x-cache',
			),
		).toBe('miss');
		expect(load).toHaveBeenCalledTimes(2);
	});
});

describe('SharedPayloadCache — the memory budget', () => {
	const big = (n: number) => new Uint8Array(n).fill(7);

	it('evicts least-recently-used to stay inside the budget', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 250 });
		const load = upstream(big(100));

		await cache.serve('a', { authorize: allow, load, maxAgeMs: CEILING });
		await cache.serve('b', { authorize: allow, load, maxAgeMs: CEILING });
		await cache.serve('a', { authorize: allow, load, maxAgeMs: CEILING }); // a is now most-recent
		await cache.serve('c', { authorize: allow, load, maxAgeMs: CEILING }); // …so b is evicted, not a

		expect(cache.stats().bytes).toBe(200);
		expect(
			(await cache.serve('a', { authorize: allow, load, maxAgeMs: CEILING })).headers.get(
				'x-cache',
			),
		).toBe('hit');
		expect(
			(await cache.serve('b', { authorize: allow, load, maxAgeMs: CEILING })).headers.get(
				'x-cache',
			),
		).toBe('miss');
	});

	it('a payload larger than the whole budget is served, not retained — and still collapses its herd', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 50 });
		const load = upstream(big(100), { delayMs: 10 });

		const [a, b] = await Promise.all([
			cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING }),
			cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING }),
		]);

		expect(a.headers.get('x-cache')).toBe('bypass');
		expect(b.headers.get('x-cache')).toBe('shared'); // the concurrent read is still deduped
		expect(load).toHaveBeenCalledTimes(1);
		expect(cache.stats().entries).toBe(0);
	});
});

describe('SharedPayloadCache — what it tells the NEXT cache', () => {
	it('never replays `public` on a response this route gated behind a session', async () => {
		// The upstream's cache-control describes OUR shared cache, not a proxy between us and the browser.
		// Replaying `public, max-age=300` verbatim invites any shared cache in front of the estate to store
		// an authorised payload and hand it to a caller who would have been refused. No leak today — the
		// Ingress runs no proxy_cache — and wrong the moment one is added, which this module's own scaling
		// note recommends.
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream(PAYLOAD, { headers: { 'cache-control': 'public, max-age=300' } });

		const res = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });

		expect(res.status).toBe(200);
		expect(res.headers.get('cache-control')).toBe('private, max-age=300');
		expect(res.headers.get('cache-control')).not.toContain('public');
	});

	it('and the warm replay says private too — a hit is the same body', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream(PAYLOAD, { headers: { 'cache-control': 'public, max-age=300' } });

		await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });
		const warm = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });

		expect(warm.headers.get('x-cache')).toBe('hit');
		expect(warm.headers.get('cache-control')).toBe('private, max-age=300');
	});

	it('a max-age with no directive still gains private — silence is not permission', async () => {
		const cache = new SharedPayloadCache({ maxBytes: 1 << 20 });
		const load = upstream(PAYLOAD, { headers: { 'cache-control': 'max-age=120' } });

		const res = await cache.serve(KEY, { authorize: allow, load, maxAgeMs: CEILING });

		expect(res.headers.get('cache-control')).toBe('private, max-age=120');
	});
});
