/**
 * The flows sandbox's inference door (`routes/api/infer/+server.ts`), exercised as a module. It
 * lives beside the engine tests because the zone's vitest scope is `src/lib/flows/**` and this route
 * is the flows' only network lane — the Model node reaches Serve through nothing else.
 *
 * What it pins: the door names no default app (a call that does not say which Serve app it wants is
 * refused, never routed to a workload the door happens to know), and the `app`/`path` guards that
 * keep it an inference door rather than an open proxy into the Serve origin.
 */
import type { RequestEvent } from '@sveltejs/kit';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$env/dynamic/private', () => ({ env: { STUDIO_SERVE_URL: 'http://serve.test:8000' } }));

const { POST } = await import('../../routes/api/infer/+server');

function call(query: string): Promise<Response> {
	const url = new URL(`http://zone.test/studio/api/infer${query}`);
	const request = new Request(url, {
		method: 'POST',
		headers: { 'content-type': 'application/octet-stream' },
		body: new Uint8Array([1, 2, 3]),
	});
	// The route reads only request/url/locals; the rest of RequestEvent is irrelevant here.
	const event = { request, url, locals: { authEnabled: false } } as unknown as RequestEvent;
	return (POST as unknown as (e: RequestEvent) => Promise<Response>)(event);
}

function stubServe() {
	const spy = vi.fn(
		async () =>
			new Response(new Uint8Array([7]), { headers: { 'content-type': 'application/json' } }),
	);
	vi.stubGlobal('fetch', spy);
	return spy;
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('POST /api/infer', () => {
	it('refuses a call that names no Serve app — the door has no default workload', async () => {
		const spy = stubServe();
		const res = await call('');
		expect(res.status).toBe(400);
		expect(await res.json()).toMatchObject({ reason: 'bad_app' });
		expect(spy).not.toHaveBeenCalled();
	});

	it('forwards to <STUDIO_SERVE_URL>/<app><path>', async () => {
		const spy = stubServe();
		const res = await call('?app=example&path=/v1/chat/completions');
		expect(res.status).toBe(200);
		expect((spy.mock.calls[0] as unknown as [string])[0]).toBe(
			'http://serve.test:8000/example/v1/chat/completions',
		);
		// A dot INSIDE a segment is an ordinary character, not a traversal.
		expect((await call('?app=example&path=/v1/model.predict')).status).toBe(200);
	});

	it('refuses an app that is not a slug and a path that could leave the app', async () => {
		const spy = stubServe();
		expect((await call('?app=..%2Fadmin')).status).toBe(400);
		expect((await call('?app=Example')).status).toBe(400);
		expect((await call('?app=example&path=/../x')).status).toBe(400);
		expect((await call('?app=example&path=/v1/..')).status).toBe(400);
		expect((await call('?app=example&path=/./x')).status).toBe(400);
		expect((await call('?app=example&path=//evil.test')).status).toBe(400);
		expect(spy).not.toHaveBeenCalled();
	});
});
