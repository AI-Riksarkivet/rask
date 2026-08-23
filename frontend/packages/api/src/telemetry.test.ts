import { describe, expect, it } from 'vitest';
import { makeTelemetryHandle, startZoneTelemetry } from './telemetry';

describe('zone telemetry', () => {
	it('is OFF unless an endpoint is configured', () => {
		// The property that keeps `make dev-zone` and every per-zone Playwright suite working with no
		// collector behind them. "Off" has to be selectable, not merely unconfigured — the Python seam
		// learned this the expensive way, where an ambient endpoint made a whole suite sleep on retries.
		expect(startZoneTelemetry({})).toBe(false);
		expect(startZoneTelemetry({ OTEL_SERVICE_NAME: 'web-home' })).toBe(false);
	});

	it('names the span by ROUTE ID, never the concrete URL', async () => {
		// A span name is a metric dimension in waiting — every span-metrics connector promotes it — so a
		// concrete URL here would mint one series per object id. That is the rule this estate has already
		// been burned by breaking at the sidecar, one layer down.
		const handle = makeTelemetryHandle('web-lakehouse');
		let seen: Request | undefined;
		const response = await handle({
			event: {
				request: new Request('http://x/lakehouse/catalog/tables/abc-123', { method: 'GET' }),
				route: { id: '/catalog/tables/[id]' },
				url: new URL('http://x/lakehouse/catalog/tables/abc-123'),
			},
			resolve: async (event: { request: Request }) => {
				seen = event.request;
				return new Response('ok', { status: 200 });
			},
		} as never);

		expect(response.status).toBe(200);
		expect(seen).toBeDefined();
	});

	it('passes a 404 through without marking the span an error', async () => {
		// A 404 is a correct answer to a wrong URL. Marking it ERROR makes every crawler a red span and
		// trains people to ignore the colour.
		const handle = makeTelemetryHandle('web-home');
		const response = await handle({
			event: {
				request: new Request('http://x/nope'),
				route: { id: null },
				url: new URL('http://x/nope'),
			},
			resolve: async () => new Response('nope', { status: 404 }),
		} as never);
		expect(response.status).toBe(404);
	});

	it('re-throws a handler failure rather than swallowing it', async () => {
		const handle = makeTelemetryHandle('web-home');
		await expect(
			handle({
				event: {
					request: new Request('http://x/boom'),
					route: { id: '/boom' },
					url: new URL('http://x/boom'),
				},
				resolve: async () => {
					throw new Error('boom');
				},
			} as never),
		).rejects.toThrow('boom');
	});
});
