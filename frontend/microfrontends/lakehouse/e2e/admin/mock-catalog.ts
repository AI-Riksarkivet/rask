// A tiny in-memory stand-in for the two catalog endpoints the admin area reaches SERVER-SIDE, where
// page.route cannot reach: `GET /v1/events` (the control-events feed's query.live poll) and `GET /v1/me`
// (the estate-admin door in admin/+layout.server.ts). Runs as a second Playwright `webServer`; the dev
// server's CATALOG_API points here. Test-control endpoints (`__mock/*`) seed events, simulate a
// governance mutation, toggle 403.
//
// /v1/me is keyed by the BEARER, never by mutable server state: the suite is fullyParallel, so a spec
// that flipped a shared "current identity" would race every other spec's admin door. session.ts mints
// the token per browser CONTEXT, so each test carries its own identity and nothing is shared.

import { ME_ADMIN, ME_MEMBER, TOKEN } from './session';
import { MOCK_CATALOG_PORT } from '../ports';

type ControlEvent = {
	event_id: string;
	occurred_at: string;
	action: string;
	object_type: string;
	object_id: string;
	actor: string | null;
	extra: Record<string, unknown>;
};

const events: ControlEvent[] = [];
let mode: 'ok' | 'forbidden' = 'ok';

/** The identity behind each e2e bearer. Unknown/absent token → 401, exactly like the real catalog. */
const IDENTITIES: Record<string, unknown> = {
	[TOKEN.admin]: ME_ADMIN,
	[TOKEN.member]: ME_MEMBER,
};

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_CATALOG_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);

		// The estate-admin door's identity lookup. `TOKEN.down` models a catalog outage, which must
		// fail CLOSED at the door rather than default open.
		if (url.pathname === '/v1/me') {
			const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');
			if (bearer === TOKEN.down) return json({ detail: 'catalog unavailable' }, 502);
			const me = IDENTITIES[bearer];
			return me ? json(me) : json({ detail: 'not authenticated' }, 401);
		}

		// The endpoint the query.live generator polls. since=N is a cursor == "events already seen"; we hand
		// back everything after it + the new head (== count), matching ControlEventBuffer.since semantics.
		if (url.pathname === '/v1/events') {
			if (mode === 'forbidden') return json({ detail: 'not authorized' }, 403);
			const since = Number(url.searchParams.get('since') ?? '0');
			return json({ events: events.slice(since), cursor: events.length, reset: false });
		}

		// ── test control plane ──
		if (url.pathname === '/__mock/add' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as Partial<ControlEvent>;
			events.push({
				event_id: `evt-${events.length + 1}`,
				occurred_at: '2026-07-23T10:30:00Z',
				action: body.action ?? 'grant_added',
				object_type: body.object_type ?? 'grant',
				object_id: body.object_id ?? 'table:db1$t',
				actor: body.actor ?? 'user:alice',
				extra: body.extra ?? {},
			});
			return json({ ok: true, cursor: events.length });
		}
		if (url.pathname === '/__mock/mode' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as { mode?: string };
			mode = body.mode === 'forbidden' ? 'forbidden' : 'ok';
			return json({ ok: true, mode });
		}
		if (url.pathname === '/__mock/reset' && req.method === 'POST') {
			events.length = 0;
			mode = 'ok';
			return json({ ok: true });
		}
		return json({ detail: 'not found' }, 404);
	},
});
