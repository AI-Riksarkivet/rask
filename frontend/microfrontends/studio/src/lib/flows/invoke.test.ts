/**
 * The Model node's network lane (`invoke.ts`) against a stubbed `/api/infer`. The door passes bytes
 * and content-type through, so this lane must too: a textual answer arrives as a text payload, and
 * anything else — an image, audio, an untyped body — arrives as BYTES carrying the upstream
 * content-type, never decoded into a string it is not.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { InvokeRequest } from './executor';

vi.mock('$app/paths', () => ({ base: '/studio' }));

const { invokeServe } = await import('./invoke');

const REQ: InvokeRequest = {
	app: 'example',
	name: '',
	path: '',
	payload: { kind: 'text', text: 'hi', mime: 'text/plain', label: 'text' },
};

function answer(res: Response) {
	const spy = vi.fn(async () => res);
	vi.stubGlobal('fetch', spy);
	return spy;
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('invokeServe — the answer keeps its type', () => {
	it.each([
		'application/json',
		'text/plain; charset=utf-8',
		'application/xml',
		'application/ld+json',
	])('%s arrives as text', async (mime) => {
		answer(new Response('{"ok":true}', { headers: { 'content-type': mime } }));
		const out = await invokeServe(REQ);
		expect(out).toEqual({ kind: 'text', text: '{"ok":true}', mime, label: 'example' });
	});

	it.each(['image/png', 'audio/wav', 'application/octet-stream'])(
		'%s arrives as bytes, untouched',
		async (mime) => {
			answer(new Response(new Uint8Array([0, 255, 1]), { headers: { 'content-type': mime } }));
			const out = await invokeServe(REQ);
			expect(out.kind).toBe('bytes');
			expect(out.mime).toBe(mime);
			expect(out.kind === 'bytes' && new Uint8Array(out.bytes)).toEqual(
				new Uint8Array([0, 255, 1]),
			);
		},
	);

	it('labels the payload with the request name when one was set', async () => {
		answer(new Response('x', { headers: { 'content-type': 'text/plain' } }));
		const out = await invokeServe({ ...REQ, name: 'probe-1' });
		expect(out.label).toBe('probe-1');
	});
});

describe('invokeServe — the request', () => {
	it('sends the payload with its own content-type, and no name unless one was set', async () => {
		const spy = answer(new Response('x', { headers: { 'content-type': 'text/plain' } }));
		await invokeServe({
			...REQ,
			payload: { kind: 'bytes', bytes: new Uint8Array([5]).buffer, mime: 'audio/flac', label: 'a' },
		});
		const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
		const qs = new URL(url, 'http://zone.test').searchParams;
		expect(qs.get('app')).toBe('example');
		expect(qs.has('name')).toBe(false);
		expect((init.headers as Record<string, string>)['content-type']).toBe('audio/flac');
	});
});

describe('invokeServe — failures read as messages', () => {
	it('an unreachable Serve names the setting to check, not a workload', async () => {
		answer(
			Response.json({ reason: 'upstream_unreachable', detail: 'fetch failed' }, { status: 503 }),
		);
		await expect(invokeServe(REQ)).rejects.toThrow(
			'Ray Serve is unreachable — is Serve listening at STUDIO_SERVE_URL?',
		);
	});

	it('an app with no HTTP ingress says so', async () => {
		answer(Response.json({ reason: 'wrong_serve_app', status: 405 }, { status: 501 }));
		await expect(invokeServe(REQ)).rejects.toThrow(
			'That Serve app has no HTTP handler (405) — pick an app whose ingress accepts HTTP requests.',
		);
	});

	it('a thrown kit error surfaces its message', async () => {
		answer(Response.json({ message: 'empty request body' }, { status: 400 }));
		await expect(invokeServe(REQ)).rejects.toThrow('empty request body');
	});
});
