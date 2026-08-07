// One mock for the two NON-catalog upstreams the admin remote functions reach server-side: GreptimeDB
// (`/v1/sql?...`, `/v1/promql*` — the audit trail + experiment metrics) and the NATS monitor (`/jsz` —
// the streams panel). Their paths never collide, so GREPTIME_API and NATS_MONITOR_API both point at
// this one port (playwright.config.ts, the auth-ON server).
//
// The medallion head (`/produce`, `/train`) was a third. No lakehouse code ever called it — the reader
// moved out with the models surfaces and was deleted with `/models/pipeline` — so MEDALLION_API is gone
// from the config rather than left pointing here.
//
// PURELY seed-driven — no baked-in fixtures: a spec seeds exactly the upstream response its old
// page.route served, keyed per bearer like mock-catalog's generic mechanism (same shape: `__mock/seed`
// {bearer, routes:{"METHOD /path?query": body|{status,body}}}, matched with-query first; mutating
// seeded hits are recorded and served back from `__mock/calls` under the caller's bearer). Unseeded
// paths 404, which every ported surface renders as its honest "unavailable" state.

import { MOCK_OBS_PORT } from '../ports';

type Body = Record<string, unknown>;

const seededByBearer = new Map<string, Map<string, unknown>>();
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

/** DEV-ONLY identity for a request that carries NO Authorization header.
 *
 *  Empty by default, so the contract every spec relies on is untouched: an absent bearer resolves to
 *  `''`, which `identityOf` does not know, and the mock 401s "exactly like the real catalog". It is set
 *  ONLY by `make dev-zone`, which runs the zone with auth OFF — no OIDC, no Dex, so the zone forwards no
 *  bearer and every seeded read would 401 into an empty state. Rather than teach the mock to accept an
 *  anonymous caller (that would trade a test's fidelity for a developer's convenience), the dev loop
 *  names an identity out of band and the mock adopts it only when told to.
 *
 *  Read once at module scope: this is process configuration, not per-request state. */
const DEV_BEARER = process.env.MOCK_DEV_BEARER ?? '';

Bun.serve({
	port: MOCK_OBS_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);
		const bearer = (req.headers.get('authorization') ?? DEV_BEARER).replace(/^Bearer /, '');

		if (url.pathname === '/__mock/seed' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; routes: Record<string, unknown> };
			const seeded = seededOf(body.bearer);
			for (const [key, value] of Object.entries(body.routes)) seeded.set(key, value);
			return json({ ok: true, seeded: seeded.size });
		}
		if (url.pathname === '/__mock/calls' && req.method === 'GET') {
			return json({ calls: callsOf(bearer) });
		}

		const seeded = seededOf(bearer);
		const hit =
			seeded.get(`${req.method} ${url.pathname}${url.search}`) ??
			seeded.get(`${req.method} ${url.pathname}`);
		if (hit !== undefined) {
			if (req.method !== 'GET') {
				const body = (await req.json().catch(() => null)) as Body | null;
				callsOf(bearer).push({ method: req.method, path: url.pathname + url.search, body });
			}
			const h = hit as { status?: unknown; body?: unknown };
			// {status, body} envelopes are detected by a NUMERIC status only — upstream payloads may carry
			// their own `status` field (Prometheus answers `status: "success"`) and must pass through.
			return h && typeof h === 'object' && typeof h.status === 'number'
				? json(h.body ?? {}, h.status)
				: json(hit);
		}
		return json({ detail: 'unseeded' }, 404);
	},
});
