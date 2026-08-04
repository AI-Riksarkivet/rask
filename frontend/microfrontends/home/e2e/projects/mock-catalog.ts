// A tiny in-memory stand-in for the CATALOG endpoints the PROJECTS surface reaches SERVER-SIDE, where
// `page.route` cannot reach: `GET /v1/me` (this zone's `+layout.server.ts` calls `fetchMe` against
// CATALOG_API directly — there is no `/capi/v1/me` browser hop to intercept the way the lakehouse zone
// had), `GET /v1/projects` (the estate listing, via this zone's own `/capi` pass-through),
// `GET /v1/projects/<p>` (the overview's `fetchProject` query), `POST /v1/warehouses` (the create that
// MINTS a project) and `POST /v1/access/tuples` (the initial-admin grant). Runs as a second Playwright
// `webServer`; the auth-ON dev server's CATALOG_API points here.
//
// The mechanism is the lakehouse mock catalog's GENERIC one, kept per-BEARER: `__mock/seed` stores
// exact responses under "METHOD /path" (with-query tried first, then without) and `__mock/calls`
// returns every mutating request that bearer's app server made. A spec seeds precisely the JSON it
// wants the catalog to answer — no per-endpoint mock code, no shared mutable state. There is no
// separate failure lever (the lakehouse's `__mock/access/config`): a denied write is just a seeded
// `{status: 403}`, which is the same mechanism and one fewer dialect.
//
// EVERYTHING is keyed by the BEARER, never by shared server state: the suite is fullyParallel, so a
// spec that flipped a shared "current identity" — or read a shared request log — would race every
// other spec. session.ts mints the token per browser CONTEXT with the test's own id appended, so each
// test carries its own identity and nothing is shared.

import { MOCK_CATALOG_PORT } from '../ports';

type Body = Record<string, unknown>;

/** Exact responses a spec seeded for ITS bearer, keyed "METHOD /path" (query included or not). */
const seededByBearer = new Map<string, Map<string, unknown>>();
/** Every non-GET that bearer's app server made — the ledger a spec reads back to pin wire bodies. */
const callsByBearer = new Map<string, Body[]>();

const seededOf = (bearer: string): Map<string, unknown> => {
	let m = seededByBearer.get(bearer);
	if (!m) {
		m = new Map();
		seededByBearer.set(bearer, m);
	}
	return m;
};

const callsOf = (bearer: string): Body[] => {
	let list = callsByBearer.get(bearer);
	if (!list) {
		list = [];
		callsByBearer.set(bearer, list);
	}
	return list;
};

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_CATALOG_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);
		const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');

		// ── test control plane ──
		if (url.pathname === '/__mock/seed' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; routes: Record<string, unknown> };
			const seeded = seededOf(body.bearer);
			for (const [key, value] of Object.entries(body.routes)) seeded.set(key, value);
			return json({ ok: true, seeded: seeded.size });
		}
		if (url.pathname === '/__mock/calls' && req.method === 'GET') {
			return json({ calls: callsOf(bearer) });
		}
		// No `__mock/reset` (the lakehouse and annotator mocks have one): a bearer is minted per TEST, so
		// every bucket here is already single-use and a reset would have no state to clear. An endpoint
		// no spec drives is test infrastructure nothing verifies.

		// ── the seeded surface ──
		// Recorded BEFORE the lookup, hit or miss: "no tuple was ever written" is only a real assertion
		// if a write the spec never seeded would still show up in the ledger.
		if (req.method !== 'GET') {
			const body = (await req.json().catch(() => null)) as Body | null;
			callsOf(bearer).push({ method: req.method, path: url.pathname + url.search, body });
		}
		const seeded = seededOf(bearer);
		const hit =
			seeded.get(`${req.method} ${url.pathname}${url.search}`) ??
			seeded.get(`${req.method} ${url.pathname}`);
		if (hit === undefined) {
			// An unseeded identity is the catalog's own answer to an unknown bearer, and the zone must
			// degrade on it (navbar signed-out, gallery told nothing) rather than 500.
			return url.pathname === '/v1/me'
				? json({ detail: 'not authenticated' }, 401)
				: json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
		}
		const shaped = hit as { status?: unknown; body?: unknown };
		// NUMERIC status only — an upstream payload carries its own `status` field (a warehouse record
		// answers `status: "active"`, Prometheus answers `status: "success"`), and a STRING one must pass
		// through as a plain body. Keying on the key's presence made `"active"` the HTTP status.
		return shaped && typeof shaped === 'object' && typeof shaped.status === 'number'
			? json(shaped.body ?? {}, shaped.status)
			: json(hit);
	},
});
