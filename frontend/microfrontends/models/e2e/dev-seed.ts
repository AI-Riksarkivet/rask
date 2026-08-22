/**
 * Dev fixtures for `make dev-zone ZONE=models` — see `lakehouse/e2e/dev-seed.ts` for the rationale.
 *
 * DEV data, not test data: no spec imports this. Shapes are copied from `registry.spec.ts` and from
 * `@rask/api/runs-feed`'s own schema, so this cannot drift into describing an API the estate does not
 * serve.
 *
 * THE LIVENESS ROUTES ARE NOT OPTIONAL — and getting this wrong is why an earlier attempt seeded five
 * data routes and still rendered nothing. This zone's registry does not read on page load; it reads on
 * a CURSOR: `ModelRegistry.svelte` ends with `liveRead(lineageTick, () => refresh())`, so `fetchModels()`
 * fires only when the lineage cursor moves. A hydrated browser proved it — the very first requests are
 *
 *     GET /events?limit=1&summary=true      (the cursor probe, `lineagePulse`)
 *     GET /runs                             (the bell's feed, same stream)
 *
 * and with both 404 the cursor never opens, `liveRead` never fires, and the table sits on "Loading…"
 * having asked the upstream for nothing at all. Seed the cursor and the data follows. (Also why a `curl`
 * check is worthless here: no hydration, no mount, no `liveRead`, zero requests.)
 */

/** The cursor probe's contract, exactly as `LineageProbeSchema` in `@rask/api/runs-feed` parses it:
 *  `{ events: [{ seq: number }] }`. A number is all that is needed — the pulse only cares that the seq
 *  RESOLVED, and re-yields an unmoved cursor on a keepalive so the stream is never severed as idle. */
export const CURSOR_PROBE = { events: [{ seq: 1 }] };

/** The bell's run rows. Failures sort first in the notification surface, so one FAILED row makes that
 *  ordering visible rather than theoretical. */
export const RUNS = {
	runs: [
		{
			run_id: 'dev-train-1',
			job: 'training.demo',
			state: 'COMPLETE',
			started_at: '2026-08-07T09:00:00Z',
			updated_at: '2026-08-07T09:04:00Z',
		},
		{
			run_id: 'dev-train-2',
			job: 'training.fraud',
			state: 'FAILED',
			started_at: '2026-08-07T08:30:00Z',
			updated_at: '2026-08-07T08:31:00Z',
		},
	],
};

/** The signed-in developer: estate admin, so privileged surfaces render rather than fail closed.
 *
 *  Seeded here unlike in the lakehouse's fixture: that zone's mock owns a dedicated `/v1/me` handler
 *  which derives the identity from the bearer, so a seed is shadowed. This zone's `mock-upstreams.ts`
 *  has no identity path — `/v1/me` is just another seeded route. */
export const DEV_ME = {
	sub: 'user:dev',
	name: 'Dev',
	email: 'dev@localhost',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

type Model = { model: string; latest_version: number; blessed_version: number | null };

const DEMO: Model = { model: 'demo', latest_version: 3, blessed_version: 2 };
const FRAUD: Model = { model: 'fraud', latest_version: 1, blessed_version: null };

/** The `/v1/model/<m>` describe body — the frozen contract `registry.spec.ts` pins, artifacts included. */
const describeOf = (m: Model, artifacts: unknown[]) => ({
	model: m.model,
	latest_version: m.latest_version,
	blessed_version: m.blessed_version,
	candidate_metrics: { rows_seen: 9, loss: 0.1234 },
	blessed_metrics: m.blessed_version ? { rows_seen: 4, loss: 0.5 } : null,
	artifacts,
});

/** A raw Prometheus MATRIX body, exactly as GreptimeDB's query_range answers.
 *
 *  `status` is the STRING "success" and must stay one: the mocks detect their `{status, body}` envelope
 *  by a NUMERIC status only, precisely so a Prometheus payload passes through as a body rather than being
 *  read as "respond 'success'". Two points, so the training curves draw a line instead of an empty state. */
const promRange = (points: [number, number][]) => ({
	status: 'success',
	data: {
		resultType: 'matrix',
		result: points.length
			? [{ metric: {}, values: points.map(([t, v]) => [t, String(v)] as [number, string]) }]
			: [],
	},
});

export const UPSTREAMS_SEED: Record<string, unknown> = {
	// LIVENESS FIRST — nothing below is ever requested without these two. See the header.
	'GET /events?limit=1&summary=true': CURSOR_PROBE,
	'GET /runs': RUNS,

	'GET /v1/me': DEV_ME,
	'GET /v1/model': { models: [DEMO, FRAUD] },
	'GET /v1/model/demo': describeOf(DEMO, [
		{ path: '3/weights.json', size_bytes: 2048, updated_at: '2026-07-24T09:00:00Z' },
		{ path: '3/scaler.json', size_bytes: 512, updated_at: null },
	]),
	'GET /v1/model/fraud': describeOf(FRAUD, []),
	'GET /v1/prometheus/api/v1/query_range': promRange([
		[1786000000, 0.42],
		[1786000600, 0.19],
	]),
	'GET /v1/prometheus/api/v1/query': promRange([]),
};

/** One mock serves every upstream this zone reads, so one group covers it (see `e2e/ports.ts`). */
export const DEV_SEEDS: {
	env: string;
	path?: string;
	body?: unknown;
	routes?: Record<string, unknown>;
}[] = [{ env: 'CATALOG_API', routes: UPSTREAMS_SEED }];
