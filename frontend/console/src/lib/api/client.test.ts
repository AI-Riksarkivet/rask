/**
 * The transport's failures, as a page reads them: what a door's problem body says in members a client
 * dispatches on, and whether a request no answer came to ran out of time or never reached the door.
 */
import { describe, expect, test } from 'vitest';
import { refusedAsProtected, upstreamJSON, type ApiFailure } from './client';

/** One request against a fetch that answers `respond()`, as its failure. */
async function failureOf(respond: () => Promise<Response>): Promise<ApiFailure> {
	const result = await upstreamJSON({
		fetch: (() => respond()) as typeof fetch,
		base: '/api/catalog',
		path: '/management/v1/projects/acme',
		init: { method: 'DELETE' },
		upstream: 'catalog',
	});
	if (result.ok) throw new Error('the request succeeded');
	return result;
}

const problem = (body: Record<string, unknown>): (() => Promise<Response>) => {
	return async () =>
		new Response(JSON.stringify(body), {
			status: Number(body['status']),
			headers: { 'content-type': 'application/problem+json' },
		});
};

describe('a refused request', () => {
	test('carries the code, the type and the extension members of the problem the door answered', async () => {
		const failure = await failureOf(
			problem({
				type: 'https://lance.org/problems/containerholdserror',
				title: 'ContainerHoldsError',
				status: 409,
				detail: "project 'acme' still holds 2 warehouse(s): acme-gold, acme-wh.",
				code: 3,
				error: "project 'acme' still holds 2 warehouse(s): acme-gold, acme-wh.",
				instance: '4bf92f3577b34da6a3ce929d0e0e4736',
				holds: ['acme-gold', 'acme-wh'],
			}),
		);

		expect(failure).toEqual({
			ok: false,
			status: 409,
			detail: "project 'acme' still holds 2 warehouse(s): acme-gold, acme-wh.",
			code: 3,
			type: 'https://lance.org/problems/containerholdserror',
			members: { holds: ['acme-gold', 'acme-wh'] },
		});
	});

	test.each([
		['a protected container', 'https://lance.org/problems/protectedcontainererror', true],
		['a protected table', 'https://lance.org/problems/protectedtableerror', true],
		// The sentence says "protected" and the type does not: the type decides.
		['a container that holds something', 'https://lance.org/problems/containerholdserror', false],
	])(
		'is a protection refusal by its type, never by its sentence: %s',
		async (_, type, isProtected) => {
			const failure = await failureOf(
				problem({
					type,
					title: 'T',
					status: 409,
					detail: 'it is protected against deletion',
					code: 3,
				}),
			);

			expect(refusedAsProtected(failure)).toBe(isProtected);
		},
	);

	test('keeps the status line as its detail when the body is no problem', async () => {
		const failure = await failureOf(
			async () => new Response('<html>bad gateway</html>', { status: 502 }),
		);

		expect(failure).toEqual({ ok: false, status: 502, detail: 'catalog answered 502' });
	});
});

describe('a request no answer came to', () => {
	test.each([
		['its own time ran out', new DOMException('The operation timed out.', 'TimeoutError'), true],
		['it was aborted', new DOMException('The operation was aborted.', 'AbortError'), true],
		['the connection was refused', new TypeError('fetch failed'), undefined],
	])('says whether the door may still have acted: %s', async (_, thrown, timedOut) => {
		const failure = await failureOf(() => Promise.reject(thrown));

		expect([failure.status, failure.timedOut]).toEqual([0, timedOut]);
	});
});
