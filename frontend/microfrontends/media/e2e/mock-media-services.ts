/**
 * A tiny in-memory stand-in for the media-plane services this zone reaches SERVER-SIDE, where
 * `page.route` cannot reach.
 *
 * Two upstreams, one process (their paths never collide, so `SEARCH_API` and `ANNOTATOR_PROJECTS_API`
 * both point here):
 *
 *   - the SEARCH service — `GET/POST /api/search`. It stays a `+server.ts` (the POST carries a File),
 *     but its RESPONSE is Arrow IPC now, and the encoder lives in the zone. Mocking the SERVICE rather
 *     than the browser fetch is what makes the promotion testable at all: a `page.route` stub would
 *     replace the very route under test, so the suite would prove nothing about the bytes the zone
 *     actually sends. Seed rows here and the real route encodes them.
 *   - the ANNOTATOR's projects plane — `/projects*`, which the send-to-project dialog now reaches
 *     through `projects.remote.ts` instead of a BFF passthrough.
 *
 * The mechanism is the GENERIC one from the lakehouse's mock catalog: `__mock/seed` stores exact
 * responses keyed by `"METHOD /path"` (with-query tried first, then without) and `__mock/calls` returns
 * every mutating request the zone's server made. A spec seeds precisely the JSON its old `page.route`
 * served — a mechanical translation, no per-endpoint mock code.
 *
 * WHERE THE LAKEHOUSE KEYS BY BEARER, THIS KEYS GLOBALLY. That zone mints a token per browser context;
 * this suite runs auth-OFF (no OIDC env → no session → no bearer at all), so there is no identity to key
 * on. What makes that safe is the surrounding discipline rather than luck: only ONE spec file
 * (`send-to-project.spec.ts`) seeds this server, playwright does not run tests from the same file in
 * parallel, and every test in it resets its own world in `beforeEach`. A second seeding file would have
 * to key by something — which is exactly why the reason is written down here rather than assumed.
 */
import { MOCK_SERVICES_PORT } from './ports';

type Body = Record<string, unknown>;
type Seeded = { status?: number; body?: unknown };

const seeded = new Map<string, unknown>();
const calls: Body[] = [];

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_SERVICES_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);

		if (url.pathname === '/__mock/seed' && req.method === 'POST') {
			const body = (await req.json()) as { routes: Record<string, unknown> };
			for (const [key, value] of Object.entries(body.routes)) seeded.set(key, value);
			return json({ ok: true, seeded: seeded.size });
		}
		if (url.pathname === '/__mock/calls' && req.method === 'GET') {
			return json({ calls });
		}
		if (url.pathname === '/__mock/reset' && req.method === 'POST') {
			seeded.clear();
			calls.length = 0;
			return json({ ok: true });
		}

		// Every mutating request is recorded BEFORE the seeded answer is chosen, so a spec can assert on
		// a call the mock refused as well as one it accepted — a 403 that was never sent and a 403 that
		// was sent and denied are different bugs.
		if (req.method !== 'GET') {
			const body = (await req.json().catch(() => null)) as Body | null;
			calls.push({ method: req.method, path: url.pathname + url.search, body });
		}

		const hit =
			seeded.get(`${req.method} ${url.pathname}${url.search}`) ??
			seeded.get(`${req.method} ${url.pathname}`);
		if (hit === undefined) return json({ detail: `unseeded: ${req.method} ${url.pathname}` }, 404);
		const h = hit as Seeded;
		return h && typeof h === 'object' && 'status' in h ? json(h.body ?? {}, h.status) : json(hit);
	},
});
