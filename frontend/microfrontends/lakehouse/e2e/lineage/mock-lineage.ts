// A tiny in-memory stand-in for the two LINEAGE endpoints this zone reaches SERVER-SIDE, where
// `page.route` cannot reach: `GET /events` (the live cursor probe every panel now hangs off) and
// `GET /runs` (the notification feed the shell's bell renders). Runs as a Playwright `webServer`; the
// auth-off dev server's `LINEAGE_API` points here.
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

/** The newest event `seq` — the cursor every live surface in the zone reads. */
let seq = 0;
let runs: Run[] = [];
/** How many probes the zone's live cursors have made — the leak detector: a generator that outlives
 *  its page keeps polling, and this is what makes that visible instead of a mystery slowdown. */
let probes = 0;

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_LINEAGE_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);

		// The cursor probe: newest first, so `events[0].seq` IS the cursor. `seq === 0` means the feed is
		// empty, which the real service also reports as an empty list.
		if (url.pathname === '/events') {
			probes += 1;
			const events = seq === 0 ? [] : [{ seq, event_type: 'COMPLETE', job: 'mock/job' }];
			return json({ events, next_cursor: null });
		}

		if (url.pathname === '/runs') return json({ runs });

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
		if (url.pathname === '/__mock/reset' && req.method === 'POST') {
			seq = 0;
			runs = [];
			return json({ ok: true });
		}

		// Same status the BFF proxy returns for an unreachable upstream — see the header comment.
		return json({ error: 'not mocked' }, 502);
	},
});
