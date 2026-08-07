/**
 * Dev fixtures for `make dev-zone ZONE=explorer` — see `lakehouse/e2e/dev-seed.ts` for the rationale.
 *
 * DEV data, not test data: no spec imports this. Shapes are copied from `zone.spec.ts` (its `HIT`,
 * `HEALTH` and `DESCRIPTOR` fixtures) and `@rask/api/runs-feed`'s `LineageProbeSchema`.
 *
 * THE DESCRIPTOR GATES EVERYTHING. This zone SSRs "Loading dataset" and renders nothing — not even the
 * search results list — until `/api/datasets/demo/descriptor` resolves. That call rides the zone's
 * `[...path]` catch-all through `makeViewerProxy`, whose upstream is `VIEWER_API` defaulting to a dead
 * `:8101` — an env NOTHING previously set: the Playwright suite works around it with `page.route` at the
 * browser boundary instead. The dev loop has no such interception, so its stack points `VIEWER_API` at
 * the mock and seeds the descriptor + health here. An earlier attempt seeded only `/api/search`, watched
 * the browser get a 200 for it, and still rendered nothing — the results list was waiting on a
 * descriptor that never came.
 *
 * SEARCH IS SEEDABLE despite answering Arrow: the Arrow lives in the ZONE, not the upstream —
 * `makeArrowRowsProxy` fetches `list[dict]` JSON from `SEARCH_API` and encodes it itself. Seed the JSON
 * and the zone produces real Arrow from it, exactly as its own specs do.
 *
 * STILL OUT OF REACH: media bytes (page images, thumbnails, chunk frames) — binary the JSON mock cannot
 * speak, so media surfaces render their unavailable state. The searchable corpus, the rail and the
 * results list all populate.
 */

/** The cursor probe's contract (`LineageProbeSchema` = `{events:[{seq:number}]}`) — see the
 *  models/annotator fixtures for why nothing hung off `liveRead` reads without it. */
export const CURSOR_PROBE = { events: [{ seq: 1 }] };

const col = (name: string, arrow_type: string) => ({
	name,
	arrow_type,
	nullable: true,
	vector_dim: null,
});

/** The corpus descriptor — the zone's boot gate. Declares FTS over `chunks.text` and one atlas space
 *  bound to `chunks` (the rail's Atlas entry is table-sensitive and hidden without it). */
export const DESCRIPTOR = {
	id: 'demo',
	tables: {
		chunks: {
			name: 'chunks',
			row_count: 3,
			version: 1,
			columns: [
				col('doc_id', 'string'),
				col('speech_id', 'int64'),
				col('chunk_id', 'int64'),
				col('title', 'string'),
				col('text', 'string'),
			],
			indexes: [],
		},
	},
	declared: {
		identity: { key_fields: ['doc_id', 'speech_id', 'chunk_id'], doc_key: 'doc_id' },
		document: null,
		time: null,
		display: { title: ['title'], body: 'text', caption: null, metadata: [] },
		search: {
			row_table: 'chunks',
			fts: { table: 'chunks', column: 'text' },
			vectors: {},
			filterable: [],
			rerank: false,
		},
		atlas: [
			{
				name: 'semantic',
				x: 'umap_x',
				y: 'umap_y',
				cluster: 'cluster',
				source_column: 'embedding',
				table: 'chunks',
				channels: [],
			},
		],
		capabilities: {},
	},
};

/** `/api/health` — the status badge's read. All-ok, so the badge is green rather than a warning that
 *  would (correctly) distract from whatever the developer is actually building. */
export const HEALTH = {
	db: { path: '/data/demo.lance', tables: ['chunks'], chunks: 3, documents: 3 },
	embed: { ok: true, url: 'http://embed', error: null },
	rerank: { ok: true, url: 'http://rerank', error: null },
};

/** Search hits in the `list[dict]` shape `makeArrowRowsProxy` re-encodes. Three, descending by score,
 *  so ordering and the score column are visible — one row shows the surface works, not that it RANKS. */
export const SEARCH_HITS = [
	{
		doc_id: 'd1',
		speech_id: 0,
		chunk_id: 0,
		title: 'Vasa inventory 1628',
		text: 'the quick brown fox jumps over the lazy dog',
		_score: 3.2,
		alignments: [],
	},
	{
		doc_id: 'd2',
		speech_id: 0,
		chunk_id: 1,
		title: 'Riksdag protocol 1734',
		text: 'resolutions concerning the naval yard',
		_score: 2.1,
		alignments: [],
	},
	{
		doc_id: 'd3',
		speech_id: 0,
		chunk_id: 2,
		title: 'Parish register 1801',
		text: 'births and deaths recorded by the vicar',
		_score: 0.9,
		alignments: [],
	},
];

export const SERVICES_SEED: Record<string, unknown> = {
	// THE BOOT GATE first, then liveness — nothing renders without the descriptor, and nothing hung
	// off `liveRead` reads without the cursor. See the header.
	'GET /api/datasets/demo/descriptor': DESCRIPTOR,
	'GET /api/health': HEALTH,
	'GET /events?limit=1&summary=true': CURSOR_PROBE,
	'GET /runs': { runs: [] },

	'GET /api/search': SEARCH_HITS,

	// The projects picker behind "send to project" — path-only, so it covers every `?tenant=`.
	'GET /projects': {
		projects: [
			{ project_id: 'p1', tenant: 'default', slug: 'vasa-portraits', title: 'Vasa portraits' },
		],
		total: 1,
	},

	// Saved views live on the catalog's user-state document; a 404 is the real "nothing saved" state.
	'GET /v1/user-state/saved-views': { status: 404, body: { detail: 'no document' } },
};

export const DEV_SEEDS: {
	env: string;
	path?: string;
	body?: unknown;
	routes?: Record<string, unknown>;
}[] = [{ env: 'SEARCH_API', routes: SERVICES_SEED }];
