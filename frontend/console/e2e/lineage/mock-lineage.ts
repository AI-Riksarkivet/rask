// A tiny in-memory stand-in for LINEAGE, served at `/api/lineage` behind the e2e edge: `GET /events`
// (the live cursor probe every panel re-reads on) and the DLQ outbox surfaces. A stateful cursor, so a
// spec can prove a panel re-reads when the estate moves, not only its first read. `__mock/bump` advances the cursor — "something
// happened in the estate" — which is exactly the event a live surface must react to and a timer cannot
// distinguish from nothing happening.
//
// Everything else answers 502, which is what the catalog door proxy itself returns when the upstream is
// unreachable — so any request a spec has NOT mocked sees the same status it saw before this server
// existed, and no spec silently changes meaning because a mock started answering.

import { MOCK_LINEAGE_PORT } from '../ports';

/** One staged event in the transactional outbox, as `/admin/dlq` reports it. */
type DlqEvent = {
	run_id: string;
	event_type: string | null;
	job: string | null;
	outputs: string[];
	inputs: string[];
	parseable: boolean;
};

/** The newest event `seq` — the cursor every live surface in the app reads. */
let seq = 0;

// ── the transport-round surfaces, keyed so parallel workers cannot collide ────────────────────────
// The outbox is per BEARER (the admin suite mints one per test — `session.ts`).
const dlqByBearer = new Map<string, DlqEvent[]>();
/** Every mutating request made with one bearer, newest last — what a spec asserts a write against. */
const callsByBearer = new Map<string, { method: string; path: string; body: unknown }[]>();

const dlqOf = (bearer: string): DlqEvent[] => {
	let list = dlqByBearer.get(bearer);
	if (!list) {
		list = [];
		dlqByBearer.set(bearer, list);
	}
	return list;
};
const callsOf = (bearer: string): { method: string; path: string; body: unknown }[] => {
	let list = callsByBearer.get(bearer);
	if (!list) {
		list = [];
		callsByBearer.set(bearer, list);
	}
	return list;
};
/** How many probes the console's live cursors have made — the leak detector: a generator that outlives
 *  its page keeps polling, and this is what makes that visible instead of a mystery slowdown. */
let probes = 0;

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

/** DEV-ONLY identity for a request that carries NO Authorization header.
 *
 *  Empty by default, so the contract every spec relies on is untouched: an absent bearer resolves to
 *  `''`, which `identityOf` does not know, and the mock 401s "exactly like the real catalog". It is set
 *  ONLY by `make dev-page`, which runs the app with auth OFF — no OIDC, no Dex, so the app forwards no
 *  bearer and every seeded read would 401 into an empty state. Rather than teach the mock to accept an
 *  anonymous caller (that would trade a test's fidelity for a developer's convenience), the dev loop
 *  names an identity out of band and the mock adopts it only when told to.
 *
 *  Read once at module scope: this is process configuration, not per-request state. */
const DEV_BEARER = process.env.MOCK_DEV_BEARER ?? '';

Bun.serve({
	port: MOCK_LINEAGE_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);
		const bearer = (req.headers.get('authorization') ?? DEV_BEARER).replace(/^Bearer /, '');

		// The cursor probe: newest first, so `events[0].seq` IS the cursor. `seq === 0` means the feed is
		// empty, which the real service also reports as an empty list.
		if (url.pathname === '/events') {
			probes += 1;
			const events = seq === 0 ? [] : [{ seq, event_type: 'COMPLETE', job: 'mock/job' }];
			return json({ events, next_cursor: null });
		}

		// ── the #83 outbox: the backlog and the replay that DRAINS it (per bearer) ──
		if (url.pathname === '/admin/dlq' && req.method === 'GET') {
			const events = dlqOf(bearer);
			return json({
				depth: events.length,
				oldest_age_seconds: 42,
				events,
				limit: Number(url.searchParams.get('limit') ?? '100'),
			});
		}
		const replay = url.pathname.match(/^\/admin\/dlq\/([^/]+)\/replay$/);
		if (replay && req.method === 'POST') {
			const runId = decodeURIComponent(replay[1]!);
			callsOf(bearer).push({ method: req.method, path: url.pathname, body: null });
			const events = dlqOf(bearer);
			const at = events.findIndex((e) => e.run_id === runId);
			if (at >= 0) events.splice(at, 1); // replayed == re-ingested + dropped from the outbox
			return json({ status: 'replayed', run_id: runId });
		}

		// ── test control plane ──
		if (url.pathname === '/__mock/bump' && req.method === 'POST') {
			seq += 1;
			return json({ ok: true, seq });
		}
		if (url.pathname === '/__mock/stats') return json({ probes, seq });
		// Seed one bearer's outbox — the events its `/admin/dlq` read returns until a replay drains them.
		if (url.pathname === '/__mock/dlq' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as { bearer?: string; events?: DlqEvent[] };
			dlqByBearer.set(body.bearer ?? '', [...(body.events ?? [])]);
			callsByBearer.delete(body.bearer ?? '');
			return json({ ok: true, depth: (body.events ?? []).length });
		}
		// What one bearer's page WROTE here — the specs' proof that a command reached the plane.
		if (url.pathname === '/__mock/calls') return json({ calls: callsOf(bearer) });
		if (url.pathname === '/__mock/reset' && req.method === 'POST') {
			seq = 0;
			return json({ ok: true });
		}

		// Same status the catalog door proxy returns for an unreachable upstream — see the header comment.
		return json({ error: 'not mocked' }, 502);
	},
});
