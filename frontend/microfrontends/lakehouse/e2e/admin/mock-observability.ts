// One mock for the three NON-catalog upstreams the admin/models remote functions reach server-side:
// GreptimeDB (`/v1/sql?...`, `/v1/promql*` — the audit trail + experiment metrics), the NATS monitor
// (`/jsz` — the streams panel) and the medallion head (`/produce`, `/train`). Their paths never
// collide, so GREPTIME_API, NATS_MONITOR_API and MEDALLION_API all point at this one port
// (playwright.config.ts, the auth-ON server).
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

Bun.serve({
	port: MOCK_OBS_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);
		const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');

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
			const h = hit as { status?: number; body?: unknown };
			return h && typeof h === 'object' && 'status' in h ? json(h.body ?? {}, h.status) : json(hit);
		}
		return json({ detail: 'unseeded' }, 404);
	},
});
