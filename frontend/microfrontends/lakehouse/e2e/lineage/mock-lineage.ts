// A tiny in-memory stand-in for the LINEAGE endpoints this zone reaches SERVER-SIDE, where
// `page.route` cannot reach: `GET /events` (the live cursor probe every panel now hangs off),
// `GET /runs` (the notification feed the shell's bell renders), and — since the transport round moved
// the DLQ pair and the governance quad onto remote functions — the outbox and dataset-governance
// surfaces those functions call. Runs as a Playwright `webServer`; BOTH dev servers' `LINEAGE_API`
// points here.
//
// It exists because the refresh trigger moved. The panels' own reads are still browser-side and still
// mocked with `page.route`; what is NOT browser-side is the cursor that decides WHEN they re-read, so
// without this the specs could only prove the first read. `__mock/bump` advances the cursor — "something
// happened in the estate" — which is exactly the event a live surface must react to and a timer cannot
// distinguish from nothing happening.
//
// Everything else answers 502, which is what the BFF proxy itself returns when the upstream is
// unreachable — so any request a spec has NOT mocked sees the same status it saw before this server
// existed, and no spec silently changes meaning because a mock started answering.

import { MOCK_LINEAGE_PORT } from '../ports';

type Run = {
	run_id: string;
	job: string | null;
	state: string | null;
	progress_done: number | null;
	progress_total: number | null;
	error_message: string | null;
	started_at: string | null;
	updated_at: string | null;
	operation: string | null;
};

/** One staged event in the transactional outbox, as `/admin/dlq` reports it. */
type DlqEvent = {
	run_id: string;
	event_type: string | null;
	job: string | null;
	outputs: string[];
	inputs: string[];
	parseable: boolean;
};

/** A dataset's governance record (#49) as `/datasets/{name}/governance` reports it. */
type Governance = {
	name: string;
	tags: string[];
	description: string | null;
	tags_updated_by: string;
	tags_updated_at: string;
	description_updated_by?: string;
	description_updated_at?: string;
};

/** The newest event `seq` — the cursor every live surface in the zone reads. */
let seq = 0;
let runs: Run[] = [];

// ── the transport-round surfaces, keyed so parallel workers cannot collide ────────────────────────
// The outbox is per BEARER (the admin suite mints one per test — `session.ts`), governance per
// DATASET (the auth-off suite has no session to key on, and a dataset name is the natural scope).
const dlqByBearer = new Map<string, DlqEvent[]>();
const governanceByDataset = new Map<string, Governance>();
/** Every mutating request a bearer's zone server made — the replacement for the `page.route` capture
 *  variables the specs held while these writes were still browser-side. */
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
const governanceOf = (dataset: string): Governance => {
	let record = governanceByDataset.get(dataset);
	if (!record) {
		record = {
			name: dataset,
			tags: [],
			description: null,
			tags_updated_by: 'alice',
			tags_updated_at: '2026-07-16T00:00:00+00:00',
		};
		governanceByDataset.set(dataset, record);
	}
	return record;
};
/** How many probes the zone's live cursors have made — the leak detector: a generator that outlives
 *  its page keeps polling, and this is what makes that visible instead of a mystery slowdown. */
let probes = 0;

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

		if (url.pathname === '/runs') return json({ runs });

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

		// ── the #49 governance record: read, tag add/remove, description (per dataset) ──
		const governanceRead = url.pathname.match(/^\/datasets\/([^/]+)\/governance$/);
		if (governanceRead) return json(governanceOf(decodeURIComponent(governanceRead[1]!)));
		const tagWrite = url.pathname.match(/^\/datasets\/([^/]+)\/tags\/([^/]+)$/);
		if (tagWrite && (req.method === 'PUT' || req.method === 'DELETE')) {
			const record = governanceOf(decodeURIComponent(tagWrite[1]!));
			const tag = decodeURIComponent(tagWrite[2]!);
			callsOf(bearer).push({ method: req.method, path: url.pathname, body: null });
			if (req.method === 'PUT' && !record.tags.includes(tag)) record.tags.push(tag);
			if (req.method === 'DELETE') record.tags = record.tags.filter((t) => t !== tag);
			return json(record);
		}
		const descriptionWrite = url.pathname.match(/^\/datasets\/([^/]+)\/description$/);
		if (descriptionWrite && req.method === 'PUT') {
			const record = governanceOf(decodeURIComponent(descriptionWrite[1]!));
			const body = (await req.json().catch(() => ({}))) as { description?: string };
			callsOf(bearer).push({ method: req.method, path: url.pathname, body });
			record.description = body.description ?? null;
			record.description_updated_by = 'alice';
			record.description_updated_at = '2026-07-16T00:00:00+00:00';
			return json(record);
		}

		// ── test control plane ──
		if (url.pathname === '/__mock/bump' && req.method === 'POST') {
			seq += 1;
			return json({ ok: true, seq });
		}
		if (url.pathname === '/__mock/runs' && req.method === 'POST') {
			runs = ((await req.json().catch(() => ({}))) as { runs?: Run[] }).runs ?? [];
			seq += 1; // a run changing state IS a lineage event
			return json({ ok: true, seq, runs: runs.length });
		}
		if (url.pathname === '/__mock/stats') return json({ probes, seq, runs: runs.length });
		// Seed one bearer's outbox — the events its `/admin/dlq` read returns until a replay drains them.
		if (url.pathname === '/__mock/dlq' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as { bearer?: string; events?: DlqEvent[] };
			dlqByBearer.set(body.bearer ?? '', [...(body.events ?? [])]);
			callsByBearer.delete(body.bearer ?? '');
			return json({ ok: true, depth: (body.events ?? []).length });
		}
		// Seed one DATASET's governance record — scoped by dataset, so the auth-off suite (which has no
		// session, and therefore no bearer) still cannot have two parallel tests share a record.
		if (url.pathname === '/__mock/governance' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as {
				dataset?: string;
				tags?: string[];
				description?: string | null;
			};
			governanceByDataset.set(body.dataset ?? '', {
				name: body.dataset ?? '',
				tags: [...(body.tags ?? [])],
				description: body.description ?? null,
				tags_updated_by: 'alice',
				tags_updated_at: '2026-07-16T00:00:00+00:00',
			});
			return json({ ok: true });
		}
		// What one bearer's zone server WROTE here — the specs' proof that a command reached the plane.
		if (url.pathname === '/__mock/calls') return json({ calls: callsOf(bearer) });
		if (url.pathname === '/__mock/reset' && req.method === 'POST') {
			seq = 0;
			runs = [];
			return json({ ok: true });
		}

		// Same status the BFF proxy returns for an unreachable upstream — see the header comment.
		return json({ error: 'not mocked' }, 502);
	},
});
