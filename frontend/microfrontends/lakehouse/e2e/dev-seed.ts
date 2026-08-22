/**
 * What `make dev-zone ZONE=lakehouse` puts INTO the mocks so the zone renders populated surfaces.
 *
 * WHY A FIXTURE FILE EXISTS AT ALL. The mocks are seed-driven and answer 404 to everything until a
 * caller POSTs `/__mock/seed` — deliberately, because a mock with baked-in fixtures cannot tell a live
 * surface from a dead one, so every spec seeds exactly the response it asserts on. That is right for
 * tests and useless for a human: `dev-zone` without this file gives working pages in their EMPTY state,
 * and "empty" is indistinguishable from "broken" when you are building a table.
 *
 * THIS IS DEV DATA, NOT TEST DATA, and the separation is the point. No spec imports it. A spec that
 * leaned on it would assert against a shape nobody declared next to the assertion — which is the
 * failure mode the seed-per-spec design exists to prevent. So this file may grow freely without any
 * risk to the suite: nothing reads it but the launcher.
 *
 * SHAPES ARE HARVESTED, NOT INVENTED. Every body below is the shape a spec already seeds for the same
 * route (`'GET /v1/table': { tables: [...] }` from `tables-preview-action.spec.ts`, the `/v1/me`
 * contract from `admin/session.ts`), so this file cannot drift into describing an API the estate does
 * not serve. When a route's real shape changes, its spec fails first — that is the intended order.
 *
 * KEYS are `"METHOD /path"`, matched with-query first and then path-only by the mocks. Values are
 * either a body, or `{status, body}` when the status is the interesting part.
 */

/** Seeds for the CATALOG mock (`CATALOG_API`).
 *
 *  NO `/v1/me` HERE, deliberately. The catalog mock owns a dedicated `/v1/me` handler that resolves the
 *  identity from the BEARER (`identityOf`, prefix-matched) and 401s an unknown one — so a seeded
 *  `GET /v1/me` is shadowed and never read. The dev loop gets its identity by handing the mock
 *  `MOCK_DEV_BEARER=e2e-token:admin` instead, which resolves to that mock's own estate-admin identity.
 *  A seed here would look authoritative and do nothing, which is worse than its absence. */
export const CATALOG_SEED: Record<string, unknown> = {
	// The tables registry. `stage$name` is the estate's table id form, so the stage cell and the
	// namespace link both have something real to render.
	'GET /v1/table': {
		tables: ['bronze$page_images', 'silver$lines', 'silver$features_daily', 'gold$htr_alto'],
	},
	// NO `GET /v1/namespace`. The catalog mounts that router with `/{id}/…` subpaths ONLY
	// (namespaces.py:55 and every decorator below it) — there is no bare list route, so a seed for it
	// is a body nothing can ever request. A seed that cannot be reached looks authoritative and does
	// nothing, which is the state this file's own header warns is worse than an absent seed.
	//
	// `WarehouseResponse` verbatim. `bucket` and `root_uri` are REQUIRED by this zone's own
	// WarehouseSchema (warehouses.remote.ts:22-30) and `serving` is a tier NAME, not a boolean. The
	// previous body carried `name`, `storage_uri` and `serving: true` — three fields the catalog never
	// returns and two the schema demands were missing — so valibot answered 502 "catalog contract
	// drift" and `make dev-zone ZONE=lakehouse` rendered the warehouses page broken. A seed is a
	// CONTRACT claim; getting it wrong is not a cosmetic fixture problem.
	'GET /v1/warehouses': [
		{
			id: 'acme-wh',
			project: 'acme',
			bucket: 'rask-wh',
			root_uri: 's3://rask-wh',
			serving: 'gold',
			status: 'active',
		},
	],
	// NO `GET /v1/stores/tiers` either: the mock DERIVES it from STORES and serves the real grouped
	// shape (`{role: Store[]}`, admin/mock-catalog.ts:187-191). A flat `{tiers: [...]}` seed would
	// shadow the correct shape with one the catalog never sends.
};

/** The runs the LINEAGE mock serves — the navbar bell's feed and the lineage run board.
 *
 *  NOT a `/__mock/seed` payload. `lineage/mock-lineage.ts` is not the generic seed-driven kind: it is
 *  a STATEFUL mock with its own API (`POST /__mock/runs` replaces its run list and bumps the cursor,
 *  because a run changing state IS a lineage event), and it answers 502 "not mocked" to everything it
 *  does not implement — which is what the BFF returns for a dead upstream, so a spec cannot mistake an
 *  unmocked route for a live one. Posting the generic envelope at it earned exactly that 502.
 *
 *  Every field of `Run` is spelled out including the nulls: the type has nine members and the board
 *  reads progress and timestamps, so a partial row renders as a half-empty row rather than an error. */
export const LINEAGE_RUNS = [
	{
		run_id: 'dev-run-1',
		job: 'medallion.bronze',
		state: 'COMPLETE',
		progress_done: 120,
		progress_total: 120,
		error_message: null,
		started_at: '2026-08-07T09:00:00Z',
		updated_at: '2026-08-07T09:02:00Z',
		operation: 'ingest-iiif',
	},
	{
		run_id: 'dev-run-2',
		job: 'medallion.silver',
		state: 'RUNNING',
		progress_done: 41,
		progress_total: 120,
		error_message: null,
		started_at: '2026-08-07T09:05:00Z',
		updated_at: '2026-08-07T09:06:30Z',
		operation: 'htr-transcribe',
	},
	{
		run_id: 'dev-run-3',
		job: 'medallion.gold',
		state: 'FAILED',
		progress_done: 3,
		progress_total: 120,
		error_message: 'alto export rejected 2 pages',
		started_at: '2026-08-07T08:40:00Z',
		updated_at: '2026-08-07T08:41:10Z',
		operation: 'alto-export',
	},
] as const;

/** Seeds for the OBSERVABILITY mock (`GREPTIME_API` + `NATS_MONITOR_API`).
 *
 *  Prometheus answers `status: "success"` — which the mocks' `{status, body}` envelope detection
 *  deliberately ignores unless `status` is NUMERIC, so this passes through as a body. That subtlety is
 *  documented in the mock and is why this seed is shaped exactly like a real Prometheus reply. */
export const OBS_SEED: Record<string, unknown> = {
	'GET /v1/prometheus/api/v1/query': {
		status: 'success',
		data: { resultType: 'vector', result: [] },
	},
	'GET /jsz': { streams: [] },
};

/**
 * Every seed group, tagged with the env var whose URL it is POSTed to. The launcher resolves the
 * address from the env it already built for the zone, so no URL or port is restated here.
 *
 * `routes` uses the GENERIC per-bearer mechanism (`POST /__mock/seed` with `{bearer, routes}`), which
 * is what the catalog and observability mocks implement. A mock with its own API declares `path` and
 * `body` instead — the lineage mock is that case, and discovering it cost a 502 rather than a wrong
 * green, which is the mock's fallback working as designed.
 */
export const DEV_SEEDS: {
	env: string;
	/** Seed endpoint. Omit for the generic `/__mock/seed`. */
	path?: string;
	/** Exact POST body. Omit to send the generic `{bearer, routes}` envelope. */
	body?: unknown;
	routes?: Record<string, unknown>;
}[] = [
	{ env: 'CATALOG_API', routes: CATALOG_SEED },
	{ env: 'GREPTIME_API', routes: OBS_SEED },
	{ env: 'LINEAGE_API', path: '/__mock/runs', body: { runs: LINEAGE_RUNS } },
];
