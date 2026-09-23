/**
 * The Serve inference door's contract, pinned against a stubbed upstream: bytes and content-type
 * pass through untouched in BOTH directions, and every failure keeps its own status — the status is
 * the payload a caller branches on. Nothing here names a workload, because the door serves any
 * Serve app: an image model, an audio model and a JSON-in/JSON-out LLM all take the same path.
 */
import type { RequestEvent } from '@sveltejs/kit';
import { isHttpError } from '@sveltejs/kit';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { proxyServeInfer, type ServeProxyOptions } from './serve-proxy';

const TARGET = 'http://serve.test:8000/example';

function event(
	body: Uint8Array<ArrayBuffer> | null,
	opts: { contentType?: string; query?: string; locals?: Record<string, unknown> } = {},
): RequestEvent {
	const url = new URL(`http://zone.test/studio/api/infer${opts.query ?? ''}`);
	const headers: Record<string, string> = {};
	if (opts.contentType) headers['content-type'] = opts.contentType;
	const request = new Request(url, { method: 'POST', headers, body });
	return { request, url, locals: opts.locals ?? {} } as unknown as RequestEvent;
}

/** Stub the global fetch with one upstream answer (a fresh clone per call, since a body reads
 *  once); returns the spy so a test can read the call. */
function upstream(answer: Response | Error) {
	const spy = vi.fn(async () => {
		if (answer instanceof Error) throw answer;
		return answer.clone();
	});
	vi.stubGlobal('fetch', spy);
	return spy;
}

async function thrown(p: Promise<unknown>): Promise<{ status: number; message: string }> {
	try {
		await p;
	} catch (err) {
		if (isHttpError(err)) return { status: err.status, message: err.body.message };
		throw err;
	}
	throw new Error('expected an HttpError, got a resolved response');
}

const run = (e: RequestEvent, opts: Partial<ServeProxyOptions> = {}) =>
	proxyServeInfer(e, { target: TARGET, ...opts });

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('proxyServeInfer — pass-through', () => {
	it('forwards the body bytes and content-type verbatim, and answers the upstream bytes and content-type verbatim', async () => {
		const spy = upstream(
			new Response(new Uint8Array([9, 8, 7]), {
				headers: { 'content-type': 'application/vnd.example+json' },
			}),
		);
		const res = await run(event(new Uint8Array([1, 2, 3, 4]), { contentType: 'audio/wav' }));

		const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
		expect(url).toBe(TARGET);
		expect(init.method).toBe('POST');
		expect((init.headers as Record<string, string>)['content-type']).toBe('audio/wav');
		expect(new Uint8Array(init.body as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3, 4]));

		expect(res.status).toBe(200);
		expect(res.headers.get('content-type')).toBe('application/vnd.example+json');
		expect(res.headers.get('cache-control')).toBe('no-store');
		expect(new Uint8Array(await res.arrayBuffer())).toEqual(new Uint8Array([9, 8, 7]));
	});

	it('labels an untyped upstream answer application/octet-stream rather than guessing a document type', async () => {
		upstream(new Response(new Uint8Array([1])));
		const res = await run(event(new Uint8Array([1]), { contentType: 'text/plain' }));
		expect(res.headers.get('content-type')).toBe('application/octet-stream');
	});

	it('labels an untyped request application/octet-stream', async () => {
		const spy = upstream(new Response(new Uint8Array([1])));
		await run(event(new Uint8Array([1])));
		const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
		expect((init.headers as Record<string, string>)['content-type']).toBe(
			'application/octet-stream',
		);
	});

	it('forwards ?name= only when the caller set one', async () => {
		const spy = upstream(new Response(new Uint8Array([1])));
		await run(event(new Uint8Array([1]), { query: '?name=a%20b' }));
		await run(event(new Uint8Array([1]), { query: '?name=' }));
		await run(event(new Uint8Array([1])));
		const urls = spy.mock.calls.map((c) => (c as unknown as [string])[0]);
		expect(urls).toEqual([`${TARGET}?name=a%20b`, TARGET, TARGET]);
	});
});

describe('proxyServeInfer — refusals the zone server makes itself', () => {
	it('refuses a signed-out caller on a governed stack before reading the body', async () => {
		const spy = upstream(new Response(new Uint8Array([1])));
		const err = await thrown(
			run(event(new Uint8Array([1]), { locals: { authEnabled: true, session: null } })),
		);
		expect(err.status).toBe(401);
		expect(spy).not.toHaveBeenCalled();
	});

	it('refuses an empty body 400 without assuming what the body should have been', async () => {
		upstream(new Response(new Uint8Array([1])));
		const err = await thrown(run(event(null)));
		expect(err).toEqual({ status: 400, message: 'empty request body' });
	});

	it('refuses a body over the ceiling 413, naming the ceiling', async () => {
		const spy = upstream(new Response(new Uint8Array([1])));
		const oneMb = 1024 * 1024;
		const err = await thrown(run(event(new Uint8Array(oneMb + 1)), { maxBytes: oneMb }));
		expect(err).toEqual({ status: 413, message: 'request body is larger than the 1 MB limit' });
		expect(spy).not.toHaveBeenCalled();
	});
});

describe('proxyServeInfer — upstream failures keep their own status', () => {
	it('no Serve on the other end → 503 upstream_unreachable', async () => {
		upstream(new TypeError('fetch failed'));
		const res = await run(event(new Uint8Array([1])));
		expect(res.status).toBe(503);
		expect(await res.json()).toMatchObject({ reason: 'upstream_unreachable', serveUrl: TARGET });
	});

	it('Serve answers 405 (an app with no HTTP ingress) → 501 wrong_serve_app', async () => {
		upstream(new Response('Method Not Allowed', { status: 405 }));
		const res = await run(event(new Uint8Array([1])));
		expect(res.status).toBe(501);
		expect(await res.json()).toMatchObject({ reason: 'wrong_serve_app', status: 405 });
	});

	it('any other upstream failure → 502 upstream_error carrying the upstream status and body', async () => {
		upstream(new Response('boom', { status: 500 }));
		const res = await run(event(new Uint8Array([1])));
		expect(res.status).toBe(502);
		expect(await res.json()).toMatchObject({
			reason: 'upstream_error',
			status: 500,
			detail: 'boom',
		});
	});
});
