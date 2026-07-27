// @rask/api/lineage — the typed browser client for the lineage plane, served through a zone's own
// same-origin `/api/*` BFF proxy.
//
// The admin, data and lineage zones each carried a byte-identical (admin/data) or near-identical
// (lineage) 111-line copy of this file. It is one client now, parameterised by the zone's `BffClient`
// (which carries the base path) — the only thing that ever actually differed between the copies.
//
// Two fetch postures, deliberately: `getJSON` swallows errors to `null` for reads where an empty state
// and an outage read the same to the user, while `requestJSON` keeps the status for anything where
// 401 ("sign in") ≠ 403 (rung denial) ≠ 0 (offline) must render differently.
import type { ApiResult, BffClient } from '../client';
import type {
	ColumnGraph,
	ColumnNeighbors,
	Creator,
	DatasetGovernance,
	DatasetSchema,
	Datasets,
	DemoDatasets,
	DlqBacklog,
	DlqReplayResponse,
	EstateGraph,
	Events,
	Jobs,
	LineageGraph,
	Namespaces,
	Neighbors,
	Producers,
	Readers,
	RunInputs,
	Runs,
	SearchResults,
} from './types';

const enc = encodeURIComponent;

export interface LineageClient {
	fetchGraph: (name: string) => Promise<LineageGraph | null>;
	/** The bulk estate read: every visible node + DERIVED_FROM edge (with per-node version/failed
	 *  rollups) in ONE request — replaces the per-dataset /producers + /graph fan-out that cost
	 *  hundreds of requests per refresh at estate scale. */
	fetchEstateGraph: (limit: number) => Promise<EstateGraph | null>;
	fetchProducers: (name: string) => Promise<Producers | null>;
	/** The read-audit query (#41): who READ this dataset. Owner-gated (stricter than /producers), so
	 *  it's status-aware — a 403 ("owner access required") must read differently from offline or an
	 *  empty log. */
	fetchReaders: (name: string) => Promise<ApiResult<Readers>>;
	fetchEvents: (opts?: {
		after?: number;
		limit?: number;
		summary?: boolean;
	}) => Promise<Events | null>;
	/** #83 DLQ ops — the transactional-outbox backlog (admin). Status-aware: a 401 on a governed stack
	 *  must read differently from an empty backlog. */
	fetchDlq: (limit?: number) => Promise<ApiResult<DlqBacklog>>;
	/** Replay one staged event — a session-only write (its own /api/admin/dlq/{run}/replay BFF route
	 *  forwards the user's bearer, never the service token). Writer-gated at the lineage service
	 *  (can_write_data on outputs). */
	replayDlq: (runId: string) => Promise<ApiResult<DlqReplayResponse>>;
	fetchDemo: () => Promise<DemoDatasets | null>;
	fetchRuns: () => Promise<Runs | null>;
	/** The inputs a run consumed, each with the version it PINNED (#115 D1 reproducibility) — "which
	 *  exact feature versions produced this output". Fetched per-run on demand (kept off the hot /runs
	 *  board to avoid N+1), so it's a plain read: null on any error, an empty list when the run pinned
	 *  nothing. */
	fetchRunInputs: (runId: string) => Promise<RunInputs | null>;
	fetchDatasets: (opts?: {
		namespace?: string;
		tag?: string;
		limit?: number;
	}) => Promise<Datasets | null>;
	/** Direct dataset-level neighbors (the backend's own /upstream + /downstream reads) — what a dataset
	 *  was derived from / what derives from it, without assembling the whole subgraph client-side. */
	fetchUpstream: (name: string) => Promise<Neighbors | null>;
	fetchDownstream: (name: string) => Promise<Neighbors | null>;
	fetchSearch: (q: string, limit?: number) => Promise<SearchResults | null>;
	fetchJobs: () => Promise<Jobs | null>;
	fetchNamespaces: () => Promise<Namespaces | null>;
	fetchColumnGraph: (name: string) => Promise<ColumnGraph | null>;
	/** Who ORIGINATED a dataset — the verified catalog principal recorded at create time (provenance,
	 *  not "who wrote the latest version"). Reader-gated like the rest of the dataset reads. */
	fetchCreator: (name: string) => Promise<Creator | null>;
	/** The persisted column schema for a dataset AT a given Lance version — time-travel (#24). Omit
	 *  version for the latest. Reader-gated. The per-version schema rides the WROTE edge, so this
	 *  answers "what did this dataset's columns look like at version N". */
	fetchSchema: (name: string, version?: number) => Promise<DatasetSchema | null>;
	/** Per-FIELD provenance (upstream) / impact (downstream) — the field-level analogue of the dataset
	 *  graph's neighbors, gated by require_metadata_access (a GET read → the read-only proxy). */
	fetchColumnUpstream: (name: string, field: string) => Promise<ColumnNeighbors | null>;
	fetchColumnDownstream: (name: string, field: string) => Promise<ColumnNeighbors | null>;
	fetchGovernance: (name: string) => Promise<DatasetGovernance | null>;
	addDatasetTag: (name: string, tag: string) => Promise<ApiResult<DatasetGovernance>>;
	removeDatasetTag: (name: string, tag: string) => Promise<ApiResult<DatasetGovernance>>;
	setDatasetDescription: (
		name: string,
		description: string,
	) => Promise<ApiResult<DatasetGovernance>>;
	/** Status-aware list reads for the DataTable pages — a governed stack without a session must render
	 *  "sign in" (401), not the same empty state as an open stack with no data (the TableRegistry
	 *  pattern). */
	listDatasets: () => Promise<ApiResult<Datasets>>;
	listJobs: () => Promise<ApiResult<Jobs>>;
	listRuns: () => Promise<ApiResult<Runs>>;
}

/** Bind the lineage client to a zone's `BffClient` (see `src/lib/api.ts` in each zone). */
export function createLineageClient({ getJSON, requestJSON }: BffClient): LineageClient {
	// Governance metadata (#49) — the writes need the status (401 sign-in vs 403 rung-denial vs
	// offline), so they use the status-aware helper instead of getJSON's null-on-error.
	const write = <T>(path: string, init: RequestInit) => requestJSON<T>('/api', path, init);

	return {
		fetchGraph: (name) => getJSON<LineageGraph>(`datasets/${enc(name)}/graph`),
		fetchEstateGraph: (limit) => getJSON<EstateGraph>(`graph?limit=${limit}`),
		fetchProducers: (name) => getJSON<Producers>(`datasets/${enc(name)}/producers`),
		fetchReaders: (name) => requestJSON<Readers>('/api', `datasets/${enc(name)}/readers`),
		fetchEvents: (opts = {}) => {
			const p = new URLSearchParams();
			if (opts.after) p.set('after', String(opts.after));
			if (opts.limit) p.set('limit', String(opts.limit));
			if (opts.summary) p.set('summary', 'true');
			const qs = p.toString();
			return getJSON<Events>(`events${qs ? `?${qs}` : ''}`);
		},
		fetchDlq: (limit = 100) => requestJSON<DlqBacklog>('/api', `admin/dlq?limit=${limit}`),
		replayDlq: (runId) =>
			requestJSON<DlqReplayResponse>('/api', `admin/dlq/${enc(runId)}/replay`, { method: 'POST' }),
		fetchDemo: () => getJSON<DemoDatasets>('demo/datasets'),
		fetchRuns: () => getJSON<Runs>('runs'),
		fetchRunInputs: (runId) => getJSON<RunInputs>(`runs/${enc(runId)}/inputs`),
		fetchDatasets: (opts = {}) => {
			const p = new URLSearchParams();
			if (opts.namespace) p.set('namespace', opts.namespace);
			if (opts.tag) p.set('tag', opts.tag);
			if (opts.limit) p.set('limit', String(opts.limit));
			const qs = p.toString();
			return getJSON<Datasets>(`datasets${qs ? `?${qs}` : ''}`);
		},
		fetchUpstream: (name) => getJSON<Neighbors>(`datasets/${enc(name)}/upstream`),
		fetchDownstream: (name) => getJSON<Neighbors>(`datasets/${enc(name)}/downstream`),
		fetchSearch: (q, limit = 10) => getJSON<SearchResults>(`search?q=${enc(q)}&limit=${limit}`),
		fetchJobs: () => getJSON<Jobs>('jobs'),
		fetchNamespaces: () => getJSON<Namespaces>('namespaces'),
		fetchColumnGraph: (name) => getJSON<ColumnGraph>(`datasets/${enc(name)}/columns`),
		fetchCreator: (name) => getJSON<Creator>(`datasets/${enc(name)}/creator`),
		fetchSchema: (name, version) =>
			getJSON<DatasetSchema>(
				`datasets/${enc(name)}/schema${version === undefined ? '' : `?version=${version}`}`,
			),
		fetchColumnUpstream: (name, field) =>
			getJSON<ColumnNeighbors>(`datasets/${enc(name)}/columns/${enc(field)}/upstream`),
		fetchColumnDownstream: (name, field) =>
			getJSON<ColumnNeighbors>(`datasets/${enc(name)}/columns/${enc(field)}/downstream`),
		fetchGovernance: (name) => getJSON<DatasetGovernance>(`datasets/${enc(name)}/governance`),
		addDatasetTag: (name, tag) =>
			write<DatasetGovernance>(`datasets/${enc(name)}/tags/${enc(tag)}`, { method: 'PUT' }),
		removeDatasetTag: (name, tag) =>
			write<DatasetGovernance>(`datasets/${enc(name)}/tags/${enc(tag)}`, { method: 'DELETE' }),
		setDatasetDescription: (name, description) =>
			write<DatasetGovernance>(`datasets/${enc(name)}/description`, {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ description }),
			}),
		listDatasets: () => requestJSON<Datasets>('/api', 'datasets?limit=500'),
		listJobs: () => requestJSON<Jobs>('/api', 'jobs'),
		listRuns: () => requestJSON<Runs>('/api', 'runs'),
	};
}
