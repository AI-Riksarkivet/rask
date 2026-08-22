// The stand-in for every upstream this zone reaches SERVER-SIDE, where `page.route` cannot follow.
//
// Three env vars point here (playwright.config.ts) because their paths never collide:
//
//   CATALOG_API   → /v1/model, /v1/model/<m>, /v1/model/<m>/promote   (models.remote.ts)
//   GREPTIME_API  → /v1/prometheus/api/v1/query[_range]               (experiments.remote.ts)
//   LINEAGE_API   → /events                                            (feeds.remote.ts — the shell's bell)
//
// A fourth, MEDALLION_API → /produce, /train, left with the `/pipeline` trigger door (deleted
// 2026-08-07). It is not re-added as "harmless": a seeded route no caller reaches is a fixture that can
// only ever pass, and it reads as coverage.
//
// ONE file, not the two the lakehouse ships. Its `mock-catalog.ts` and `mock-observability.ts` are two
// different servers because the catalog one also serves an estate-admin door, a control-event feed and a
// stores registry — surfaces this zone does not have. Trim those away and both files reduce to the SAME
// generic mechanism, so shipping two copies of it here would be duplication with a second port attached.
// The lakehouse's own observability mock already collapses three upstreams onto one port for exactly this
// reason; this is that argument applied once more.
//
// PURELY seed-driven — no baked-in fixtures. A spec seeds exactly the upstream response it needs, keyed
// per bearer: `__mock/seed` {bearer, routes:{"METHOD /path?query": body|{status,body}}}, matched
// with-query first and then path-only. Mutating hits are recorded and served back from `__mock/calls`
// under the caller's bearer — the replacement for the capture variables a page.route-era spec held.
// Unseeded paths 404, which every surface here renders as its own honest "unavailable" state (and which
// is what the lineage probe behind the bell gets: it fails quiet, so the bell simply stays empty).
//
// EVERYTHING mutable is keyed by the BEARER, never by shared server state: the suite is fullyParallel, so
// a shared seed map or a shared request log would race every other spec.

import { MOCK_UPSTREAMS_PORT } from './ports';

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

const DEV_BEARER = process.env.MOCK_DEV_BEARER ?? '';

Bun.serve({
	port: MOCK_UPSTREAMS_PORT,
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
			// their own `status` field (Prometheus answers `status: "success"`) and must pass through as a
			// body rather than being read as "respond 'success'".
			return h && typeof h === 'object' && typeof h.status === 'number'
				? json(h.body ?? {}, h.status)
				: json(hit);
		}
		return json({ detail: 'unseeded' }, 404);
	},
});
