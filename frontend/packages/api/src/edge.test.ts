import { describe, expect, it } from 'vitest';
import { makeEdgeHandleFetch } from './edge';

const origin = 'https://app.example.com';
const args = (url: string, capture: (req: Request) => void) => ({
	event: { url: new URL(origin) },
	request: new Request(url),
	fetch: (async (input: Request | string | URL) => {
		capture(input instanceof Request ? input : new Request(input));
		return new Response('ok');
	}) as typeof fetch,
});

describe('makeEdgeHandleFetch', () => {
	it('rewrites same-origin /api/* SSR fetches to the edge base', async () => {
		let seen = '';
		const handle = makeEdgeHandleFetch('http://edge:8080');
		await handle(args(`${origin}/api/runs`, (r) => (seen = r.url)));
		expect(seen).toBe('http://edge:8080/api/runs');
	});

	it('trims a trailing slash on the edge base', async () => {
		let seen = '';
		const handle = makeEdgeHandleFetch('http://edge:8080/');
		await handle(args(`${origin}/api/x?q=1`, (r) => (seen = r.url)));
		expect(seen).toBe('http://edge:8080/api/x?q=1');
	});

	it('passes non-/api and cross-origin requests through unchanged', async () => {
		let seen = '';
		const handle = makeEdgeHandleFetch('http://edge:8080');
		await handle(args(`${origin}/assets/logo.svg`, (r) => (seen = r.url)));
		expect(seen).toBe(`${origin}/assets/logo.svg`);
	});

	it('is a no-op when the edge url is falsy', async () => {
		let seen = '';
		const handle = makeEdgeHandleFetch(undefined);
		await handle(args(`${origin}/api/runs`, (r) => (seen = r.url)));
		expect(seen).toBe(`${origin}/api/runs`);
	});
});
