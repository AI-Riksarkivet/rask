import { beforeEach, describe, expect, it } from 'vitest';
import { ApiError, getHealth, upstreamFor } from './api';
import { resetApiBase, setApiBase } from './base';

/**
 * A proxy error must name the service that failed.
 *
 * The media plane fans out to THREE backends — viewer, search, annotator (plus assist) — and a reverse
 * proxy answering 502 carries no problem+json body, so `statusText` was all the message had: `api 502:
 * Bad Gateway`. Seen for real on the deployed annotator, which rendered exactly that with a Retry button
 * and no documents. The cause was the VIEWER being OOM-killed (exitCode 137) while serving thumbnails,
 * and nothing in the message pointed there — with three upstreams, an operator has nowhere to start.
 *
 * Two halves, deliberately. The mapping is asserted DIRECTLY, because it is the routing table and a moved
 * BFF route should redden one obvious line. The message shaping is driven through a public call with an
 * injected `fetcher`, so it is exercised at the same seam a real failure arrives on. `getHealth` is the
 * vehicle because it needs no loaded dataset descriptor — `search` and most others call `activeView()` and
 * would need a descriptor fixture that has nothing to do with what is being asserted here.
 */
beforeEach(() => resetApiBase());

/** A fetch that answers one status, with `url` set the way a real Response carries it. */
function failWith(status: number, statusText: string, body?: unknown): typeof fetch {
	return ((input: RequestInfo | URL) => {
		const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
		const r = new Response(body === undefined ? null : JSON.stringify(body), {
			status,
			statusText,
			headers: { 'content-type': 'application/problem+json' },
		});
		Object.defineProperty(r, 'url', { value: `http://x${url}` });
		return Promise.resolve(r);
	}) as unknown as typeof fetch;
}

async function messageOf(run: () => Promise<unknown>): Promise<string> {
	try {
		await run();
	} catch (e) {
		expect(e).toBeInstanceOf(ApiError);
		return (e as ApiError).message;
	}
	throw new Error('expected the call to reject');
}

describe('the path → service mapping follows the BFF routes', () => {
	// Asserted directly because it IS the routing table: the specific domains, then the catch-all. If a BFF
	// route moves zones or changes prefix, this is the line that should go red.
	it.each([
		['/media/api/search', 'search'],
		['/media/api/annotations/tags', 'annotator'],
		['/annotator/api/annotations/abc', 'annotator'],
		['/annotator/api/jobs/apply', 'annotator'],
		['/annotator/api/assist/suggest', 'assist'],
		['/annotator/api/config', 'assist'],
		// No dedicated route → `api/[...path]`, which is makeViewerProxy in BOTH zones.
		['/media/api/health', 'viewer'],
		['/media/api/thumbnail/abc', 'viewer'],
		['/annotator/api/documents?page=1', 'viewer'],
		['/media/api/atlas/chunks', 'viewer'],
	])('%s → %s', (path, service) => {
		expect(upstreamFor(path)).toBe(service);
	});

	it('attributes nothing outside /api/', () => {
		expect(upstreamFor('/media/not-the-api')).toBeNull();
		expect(upstreamFor('/lakehouse/data')).toBeNull();
	});
});

describe('a gateway failure names the upstream that did not answer', () => {
	it('a catch-all path → the viewer, which is the service that actually OOM-ed', async () => {
		setApiBase('/annotator');
		const message = await messageOf(() => getHealth(failWith(502, 'Bad Gateway')));
		// /api/health has no dedicated route: it falls to `api/[...path]`, which is makeViewerProxy in both
		// zones. Defaulting to the viewer therefore follows the routing table, not a guess.
		expect(message).toContain('viewer service did not respond');
		expect(message).toContain('/annotator/api/health');
	});

	it('covers 503 and 504, not only 502', async () => {
		setApiBase('/media');
		for (const [status, text] of [
			[503, 'Service Unavailable'],
			[504, 'Gateway Timeout'],
		] as const) {
			const message = await messageOf(() => getHealth(failWith(status, text)));
			expect(message, `${status} should name the service`).toContain('viewer service');
		}
	});
});

describe('it does not talk over the backend when the backend DID answer', () => {
	it('a problem+json detail wins — the service explained itself', async () => {
		setApiBase('/media');
		const message = await messageOf(() =>
			getHealth(failWith(502, 'Bad Gateway', { detail: 'index rebuild in progress' })),
		);
		expect(message).toContain('index rebuild in progress');
		expect(message).not.toContain('did not respond');
	});

	it('a non-gateway status keeps the plain form', async () => {
		setApiBase('/media');
		const message = await messageOf(() => getHealth(failWith(500, 'Internal Server Error')));
		// 500 means the service ANSWERED and failed inside it. Calling that "did not respond" would be a
		// lie, and would send an operator to check a pod that is running fine.
		expect(message).not.toContain('did not respond');
		expect(message).toContain('500');
	});

	it('a non-/api path is not attributed to any service', async () => {
		setApiBase('');
		const message = await messageOf(() =>
			getHealth(((input: RequestInfo | URL) => {
				void input;
				const r = new Response(null, { status: 502, statusText: 'Bad Gateway' });
				Object.defineProperty(r, 'url', { value: 'http://x/not-the-api' });
				return Promise.resolve(r);
			}) as unknown as typeof fetch),
		);
		expect(message).toContain('upstream did not respond');
		expect(message).not.toContain('viewer');
	});
});
