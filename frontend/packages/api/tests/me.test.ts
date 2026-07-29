/**
 * Unit tests for `fetchMe` — the single-sourced BFF fetch of the frozen `GET /v1/me` contract. Every
 * zone's navbar identity flows through this one helper, so its degrade-to-null posture (no token, 401,
 * outage, timeout, schema drift) is pinned here rather than re-proven per zone.
 */
import { describe, expect, test } from 'vitest';
import { fetchMe, type Me } from '../src/me';

const me: Me = {
	sub: 'user:alice',
	name: 'Alice Ackerman',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

/** A fake fetch that records the request and returns a canned response. */
function fakeFetch(status: number, body: unknown) {
	const calls: { url: string; headers: Record<string, string> }[] = [];
	const fetchFn = (async (input: URL | RequestInfo, init?: RequestInit) => {
		calls.push({
			url: String(input),
			headers: (init?.headers ?? {}) as Record<string, string>,
		});
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': 'application/json' },
		});
	}) as typeof fetch;
	return { fetchFn, calls };
}

describe('fetchMe', () => {
	test('forwards the bearer to <catalogUrl>/v1/me and parses the frozen shape', async () => {
		const { fetchFn, calls } = fakeFetch(200, me);
		const got = await fetchMe({ catalogUrl: 'http://catalog:2333/', accessToken: 'tok', fetchFn });
		expect(got).toEqual(me);
		expect(calls[0]?.url).toBe('http://catalog:2333/v1/me');
		expect(calls[0]?.headers['authorization']).toBe('Bearer tok');
	});

	test('resolves null without a network call when there is no session token', async () => {
		const { fetchFn, calls } = fakeFetch(200, me);
		expect(await fetchMe({ catalogUrl: 'http://catalog:2333', fetchFn })).toBeNull();
		expect(await fetchMe({ catalogUrl: 'http://c', accessToken: null, fetchFn })).toBeNull();
		expect(calls).toHaveLength(0);
	});

	test('resolves null on a non-2xx (the anon/expired 401 path)', async () => {
		const { fetchFn } = fakeFetch(401, { error: 'unauthorized' });
		expect(await fetchMe({ catalogUrl: 'http://c', accessToken: 'tok', fetchFn })).toBeNull();
	});

	test('resolves null when the response drifts from the frozen contract', async () => {
		const { fetchFn } = fakeFetch(200, { sub: 'user:alice', estate_admin: 'yes' });
		expect(await fetchMe({ catalogUrl: 'http://c', accessToken: 'tok', fetchFn })).toBeNull();
	});

	test('resolves null on a network failure instead of throwing', async () => {
		const fetchFn = (async () => {
			throw new TypeError('fetch failed');
		}) as typeof fetch;
		expect(await fetchMe({ catalogUrl: 'http://c', accessToken: 'tok', fetchFn })).toBeNull();
	});

	test('resolves null when the fetch outlives the timeout budget', async () => {
		const fetchFn = (async (_input: URL | RequestInfo, init?: RequestInit) =>
			new Promise<Response>((_resolve, reject) => {
				init?.signal?.addEventListener('abort', () => reject(new DOMException('', 'TimeoutError')));
			})) as typeof fetch;
		expect(
			await fetchMe({ catalogUrl: 'http://c', accessToken: 'tok', fetchFn, timeoutMs: 20 }),
		).toBeNull();
	});
});
