// A tiny in-memory stand-in for the ANNOTATOR service endpoints this zone reaches SERVER-side, where
// `page.route` cannot reach: the whole projects/tasks plane and the interactive assist call, which
// became remote functions in the transport migration (the transport ruling, area 4). Runs as a second
// Playwright `webServer`; both dev servers' ANNOTATOR_API / ANNOTATOR_PROJECTS_API point here.
//
// The mechanism is the lakehouse mock catalog's GENERIC one, minus its per-bearer keying: `__mock/seed`
// stores exact responses under "METHOD /path" (with-query tried first, then without) and `__mock/calls`
// returns every mutating request the app server made. A spec seeds precisely the JSON its old
// `page.route` handler served — a mechanical translation, no per-endpoint mock code.
//
// WHY THE STORE IS GLOBAL, and what makes that safe: this zone's e2e runs with auth OFF (no OIDC env),
// so every request arrives with NO bearer — there is no per-test identity to key on the way the
// lakehouse admin suite does. The safety comes from the runner instead: playwright.config.ts pins
// `workers: 1`, so exactly one spec drives this server at a time, and each spec resets its ledger in
// `beforeEach`. If that cap is ever lifted, this store must be keyed (a per-test project/task id
// prefix is the cheapest key) BEFORE the suite goes parallel — seeds would otherwise cross tests
// silently, which is a green suite testing another test's world.

import { MOCK_ANNOTATOR_PORT } from './ports';

type Body = Record<string, unknown>;

/** Exact responses a spec seeded, keyed "METHOD /path" (query included or not). */
const seeded = new Map<string, unknown>();
/** Every non-GET the app server made — the spec's replacement for the capture variables its
 *  `page.route` handlers used to fill while the transport was browser-side. */
const calls: Body[] = [];

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_ANNOTATOR_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);

		// ── test control plane ──
		if (url.pathname === '/__mock/seed' && req.method === 'POST') {
			const body = (await req.json()) as { routes: Record<string, unknown> };
			for (const [key, value] of Object.entries(body.routes)) seeded.set(key, value);
			return json({ ok: true, seeded: seeded.size });
		}
		if (url.pathname === '/__mock/calls' && req.method === 'GET') return json({ calls });
		if (url.pathname === '/__mock/reset' && req.method === 'POST') {
			seeded.clear();
			calls.length = 0;
			return json({ ok: true });
		}

		// ── the seeded surface ──
		const body =
			req.method === 'GET' ? null : ((await req.json().catch(() => null)) as Body | null);
		if (req.method !== 'GET') {
			calls.push({ method: req.method, path: url.pathname + url.search, body });
		}
		const hit =
			seeded.get(`${req.method} ${url.pathname}${url.search}`) ??
			seeded.get(`${req.method} ${url.pathname}`);
		if (hit === undefined) return json({ detail: 'unstubbed' }, 404);
		// A seed may name a FILE instead of a JSON body. The real service answers the annotations
		// plane in Arrow IPC, and a mock that can only speak JSON cannot stand in for it — which
		// matters beyond fidelity: `playwright-cli` can only send text bodies (`--body`), so without
		// this there is no way to drive a canvas surface from the CLI at all, only from the runner.
		const filed = hit as {
			file?: unknown;
			contentType?: unknown;
			headers?: Record<string, string>;
		};
		if (filed && typeof filed === 'object' && typeof filed.file === 'string') {
			return new Response(Bun.file(filed.file), {
				headers: {
					'content-type':
						typeof filed.contentType === 'string' ? filed.contentType : 'application/octet-stream',
					...filed.headers,
				},
			});
		}
		// A NUMERIC `status` marks the {status, body} response shape. The check must be on the
		// type, not on key presence: a seeded BODY legitimately carries its own string `status`
		// field (the jobs plane's JobResult.status='queued' is one), and treating that as an HTTP
		// status made `new Response` throw — every seeded jobs route answered 500.
		const shaped = hit as { status?: unknown; body?: unknown };
		return shaped && typeof shaped === 'object' && typeof shaped.status === 'number'
			? json(shaped.body ?? {}, shaped.status)
			: json(hit);
	},
});
