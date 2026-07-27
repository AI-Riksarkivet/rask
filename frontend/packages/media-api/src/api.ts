/**
 * Typed client for the schema-agnostic FastAPI backend.
 *
 * Response envelopes are zod-defined + runtime-validated here, so backend
 * drift surfaces as a clean error instead of silent rendering bugs. The
 * schemas describe transport envelopes (ranking signals, alignments, atlas
 * wire format, graph/voice/topic shapes) — NEVER a corpus's column names.
 * Per-row field access goes through the {@link DatasetView} (see
 * `$lib/descriptor`), which every renderer reads instead of hardcoding fields.
 */

import { tableFromIPC, type Vector } from 'apache-arrow';
import { UrlMemo } from './memo';
import * as v from 'valibot';

import { apiUrl } from './base';
import {
	activeView,
	AlignmentSchema,
	type Alignment,
	type DatasetView,
	type Row,
	RowSchema,
	type SearchMode,
} from './descriptor';

export type { Alignment, Row, SearchMode } from './descriptor';
export { activeView } from './descriptor';
export { apiUrl, setApiBase } from './base';

/** Legacy alias — a search/browse result row. Field access goes through the
 *  active {@link DatasetView}; this stays for import compatibility. */
export type Hit = Row;

// ─────────────────────────────────────────────────────────────────────
// Relevance normalization (pure — reads only the ranking signals)
// ─────────────────────────────────────────────────────────────────────

const COSINE_DISTANCE_MAX = 2;

/** A single comparable relevance number for a hit, normalized so higher is
 *  always better; `null` when the hit carries no ranking signal. */
export function relevanceOf(hit: Hit, mode?: SearchMode): number | null {
	switch (mode) {
		case 'fts':
		case 'scene_fts':
			return hit._score ?? null;
		case 'semantic':
		case 'visual':
			return hit._distance != null ? COSINE_DISTANCE_MAX - hit._distance : null;
		case 'hybrid':
			return hit._relevance_score ?? null;
		case 'scene':
			return null;
		default:
			if (hit._relevance_score != null) return hit._relevance_score;
			if (hit._score != null) return hit._score;
			if (hit._distance != null) return COSINE_DISTANCE_MAX - hit._distance;
			return null;
	}
}

// ─────────────────────────────────────────────────────────────────────
// Search request shape
// ─────────────────────────────────────────────────────────────────────

// Optional fields are written `T | undefined` (exactOptionalPropertyTypes).
export interface SearchSpec {
	q: string;
	n?: number | undefined;
	mode?: SearchMode | undefined;
	rerank?: boolean | undefined;
	rerankN?: number | undefined;
	fuzziness?: (0 | 1 | 2) | undefined;
	phrase?: boolean | undefined;
	/** Hybrid weight ∈ [0,1]: 0 = pure FTS, 1 = pure vector. Undefined = RRF. */
	weight?: number | undefined;
	qVec?: string | undefined;
	where?: string | undefined;
	prefilter?: boolean | undefined;
	/** Structured metadata filters keyed by descriptor filterable field name. */
	filters?: Record<string, string> | undefined;
	/** Topic browse token — matches the dataset's topic layers server-side. */
	topic?: string | undefined;
	image?: File | null | undefined;
	/** Non-default dataset id; omitted for the default DB. */
	dataset?: string | undefined;
}

// ─────────────────────────────────────────────────────────────────────
// Fetch wrappers
// ─────────────────────────────────────────────────────────────────────

export class ApiError extends Error {
	constructor(
		public readonly status: number,
		public readonly detail: string,
	) {
		super(`api ${status}: ${detail}`);
		this.name = 'ApiError';
	}
}

// RFC 9457 problem+json: DomainError → { detail, title }; FastAPI 422 →
// { title, errors:[...] } with no string `detail`. Parse both keys.
const ProblemSchema = v.object({ detail: v.optional(v.string()), title: v.optional(v.string()) });

/** Which backend a BFF path proxies to. The media plane fans out to THREE services, so a bare
 *  "502 Bad Gateway" tells an operator nothing — it does not even say which one to go look at.
 *
 *  Seen for real: the annotator rendered `api 502: Bad Gateway` with a Retry button and no documents,
 *  and the cause was the VIEWER being OOM-killed while serving thumbnails. Nothing in the message
 *  pointed there. Derived from the request path rather than threaded through every call site, because
 *  the path is already on the Response and the mapping is a property of the BFF routes, not of callers.
 *  Kept in sync with `microfrontends/{media,annotator}/src/routes/api/**`. */
export function upstreamFor(path: string): string | null {
	const api = path.replace(/^\/(media|annotator)/, '');
	if (!api.startsWith('/api/')) return null;
	// Mirrors the BFF's own route shape: a few specific domains, and a catch-all. The catch-all
	// (`api/[...path]/+server.ts`) is `makeViewerProxy(env)` in BOTH zones, so defaulting to the viewer is
	// the routing table rather than a guess — and it means a newly added viewer path is named correctly
	// without touching this function.
	if (api.startsWith('/api/search')) return 'search';
	if (api.startsWith('/api/annotations') || api.startsWith('/api/jobs')) return 'annotator';
	if (api.startsWith('/api/assist') || api.startsWith('/api/config')) return 'assist';
	return 'viewer';
}

/** 502/503/504 from a reverse proxy carry no problem+json body — the upstream never answered, so there
 *  is nothing to parse and `statusText` is all that is left ("Bad Gateway"). That is the one case where
 *  naming the service is the whole value of the message. */
const GATEWAY_STATUSES = new Set([502, 503, 504]);

async function apiErrorFrom(r: Response): Promise<ApiError> {
	const body: unknown = await r.json().catch(() => null);
	const parsed = v.safeParse(ProblemSchema, body);
	const fromBody = parsed.success ? (parsed.output.detail ?? parsed.output.title) : undefined;
	if (!fromBody && GATEWAY_STATUSES.has(r.status)) {
		let path = '';
		try {
			path = new URL(r.url).pathname;
		} catch {
			path = r.url; // a relative or malformed url: still better in the message than nothing
		}
		const service = upstreamFor(path);
		return new ApiError(
			r.status,
			service
				? `the ${service} service did not respond (${r.statusText || r.status} at ${path})`
				: `upstream did not respond (${r.statusText || r.status} at ${path})`,
		);
	}
	return new ApiError(r.status, fromBody || r.statusText || `HTTP ${r.status}`);
}

async function asJson<T>(r: Response, schema: v.GenericSchema<T>): Promise<T> {
	if (!r.ok) throw await apiErrorFrom(r);
	return v.parse(schema, await r.json());
}

const HitsArraySchema = v.array(RowSchema) as v.GenericSchema<Row[]>;

/** Append the `dataset` selector to a params bag when a non-default dataset is
 *  active or the spec names one. */
function datasetParam(spec?: SearchSpec): string | null {
	if (spec?.dataset) return spec.dataset;
	return activeView().datasetParam();
}

function appendCommonSearchParams(
	out: { append(name: string, value: string): void },
	spec: SearchSpec,
): void {
	if (spec.rerank) out.append('rerank', 'true');
	if (spec.rerank && spec.rerankN !== undefined) out.append('rerank_n', String(spec.rerankN));
	if (spec.weight !== undefined) out.append('weight', String(spec.weight));
	if (spec.qVec) out.append('q_vec', spec.qVec);
	if (spec.where) out.append('where', spec.where);
	if (spec.prefilter === false) out.append('prefilter', 'false');
	// Descriptor-declared filterable fields, marshalled by their own names.
	for (const [field, value] of Object.entries(spec.filters ?? {})) {
		if (value) out.append(field, value);
	}
	if (spec.topic) out.append('topic', spec.topic);
	const ds = datasetParam(spec);
	if (ds) out.append('dataset', ds);
}

/** Run a search. POST + multipart when an image is attached; GET otherwise. */
export async function search(spec: SearchSpec, fetcher: typeof fetch = fetch): Promise<Row[]> {
	const n = String(spec.n ?? 30);
	const mode = spec.mode ?? 'fts';

	if (spec.image) {
		const fd = new FormData();
		fd.append('image', spec.image);
		if (spec.q) fd.append('q', spec.q);
		fd.append('n', n);
		fd.append('mode', mode);
		appendCommonSearchParams(fd, spec);
		const r = await fetcher(apiUrl('/api/search'), { method: 'POST', body: fd });
		return asJson(r, HitsArraySchema);
	}

	const params = new URLSearchParams({ q: spec.q, n, mode });
	if (spec.fuzziness) params.append('fuzziness', String(spec.fuzziness));
	if (spec.phrase) params.append('phrase', 'true');
	appendCommonSearchParams(params, spec);
	const r = await fetcher(apiUrl(`/api/search?${params}`));
	return asJson(r, HitsArraySchema);
}

/** `?dataset=` suffix for a bare GET URL (empty for the default dataset). */
function datasetSuffix(): string {
	const ds = activeView().datasetParam();
	return ds ? `?dataset=${encodeURIComponent(ds)}` : '';
}

const ChunkAlignmentsSchema = v.object({ alignments: v.array(AlignmentSchema) });

/** Per-word alignments for one row, fetched on demand when opened in the player.
 *  Search results ship `alignments: []` (the timing blob dominates the payload).
 *  Path arity follows the dataset's identity key fields. */
export async function getChunkAlignments(
	keys: (string | number)[],
	fetcher: typeof fetch = fetch,
): Promise<Alignment[]> {
	const path = keys.map((k) => encodeURIComponent(String(k))).join('/');
	const r = await fetcher(apiUrl(`/api/chunk-alignments/${path}${datasetSuffix()}`));
	const data = await asJson(r, ChunkAlignmentsSchema);
	return data.alignments;
}

const DocTranscriptChunkSchema = RowSchema;
export type DocTranscriptChunk = Row;

const DocTranscriptSchema = v.object({
	doc_id: v.string(),
	chunks: v.array(DocTranscriptChunkSchema),
});
export type DocTranscript = v.InferOutput<typeof DocTranscriptSchema>;

// Bounded LRU of in-flight/resolved transcripts (immutable per doc, heavy
// payload) so re-opening any row in the same doc is instant.
const MAX_DOC_TRANSCRIPTS = 50;
const docTranscriptCache = new Map<string, Promise<DocTranscript>>();

/** Whole-document transcript, chunk-segmented + ordered. Lazy-fetched when a
 *  row opens so playback past the selected chunk still has karaoke. */
export async function getDocTranscript(
	docId: string,
	fetcher: typeof fetch = fetch,
): Promise<DocTranscript> {
	const suffix = datasetSuffix();
	const fetchOnce = async (): Promise<DocTranscript> => {
		const r = await fetcher(apiUrl(`/api/doc-transcript/${encodeURIComponent(docId)}${suffix}`));
		return asJson(r, DocTranscriptSchema);
	};
	if (fetcher !== fetch) return fetchOnce();
	const cacheKey = `${activeView().id}/${docId}`;
	const cached = docTranscriptCache.get(cacheKey);
	if (cached) {
		docTranscriptCache.delete(cacheKey);
		docTranscriptCache.set(cacheKey, cached);
		return cached;
	}
	const p: Promise<DocTranscript> = fetchOnce().catch((e: unknown) => {
		if (docTranscriptCache.get(cacheKey) === p) docTranscriptCache.delete(cacheKey);
		throw e;
	});
	docTranscriptCache.set(cacheKey, p);
	if (docTranscriptCache.size > MAX_DOC_TRANSCRIPTS) {
		const oldest = docTranscriptCache.keys().next().value;
		if (oldest !== undefined) docTranscriptCache.delete(oldest);
	}
	return p;
}

// ── Diarization (Speakers tab, capability-gated) ────────────────────────────
const DiarTurnSchema = v.object({
	turn_id: v.pipe(v.number(), v.integer()),
	speaker: v.string(),
	start: v.number(),
	end: v.number(),
});
export type DiarTurn = v.InferOutput<typeof DiarTurnSchema>;

const DiarizationResponseSchema = v.object({
	built: v.boolean(),
	doc_id: v.string(),
	turns: v.array(DiarTurnSchema),
	speakers: v.array(v.string()),
});
export type DiarizationResponse = v.InferOutput<typeof DiarizationResponseSchema>;

export async function getDiarization(
	docId: string,
	fetcher: typeof fetch = fetch,
): Promise<DiarizationResponse> {
	const r = await fetcher(
		apiUrl(`/api/diarization/${encodeURIComponent(docId)}${datasetSuffix()}`),
	);
	return asJson(r, DiarizationResponseSchema);
}

// ── Health ──────────────────────────────────────────────────────────────
const PingSchema = v.object({ ok: v.boolean(), url: v.string(), error: v.nullish(v.string()) });
const HealthSchema = v.object({
	db: v.object({
		path: v.string(),
		tables: v.array(v.string()),
		chunks: v.number(),
		documents: v.number(),
	}),
	embed: PingSchema,
	rerank: PingSchema,
});
export type Health = v.InferOutput<typeof HealthSchema>;

export async function getHealth(fetcher: typeof fetch = fetch): Promise<Health> {
	const r = await fetcher(apiUrl('/api/health'));
	return asJson(r, HealthSchema);
}

// ── Documents gallery (row envelope — corpus fields read via the view) ──────
const DocumentSchema = RowSchema;
export type Document = Row;

const DocumentsResponseSchema = v.object({
	total: v.pipe(v.number(), v.integer()),
	page: v.pipe(v.number(), v.integer()),
	docs: v.array(DocumentSchema),
});
export type DocumentsResponse = v.InferOutput<typeof DocumentsResponseSchema>;

export async function listDocuments(
	page = 1,
	perPage = 24,
	fetcher: typeof fetch = fetch,
): Promise<DocumentsResponse> {
	const suffix = activeView().datasetParam();
	const ds = suffix ? `&dataset=${encodeURIComponent(suffix)}` : '';
	const r = await fetcher(apiUrl(`/api/documents?page=${page}&per_page=${perPage}${ds}`));
	return asJson(r, DocumentsResponseSchema);
}

// ── Filterable columns ───────────────────────────────────────────────────
const ColumnSchema = v.object({ name: v.string(), type: v.string() });
export type ColumnInfo = v.InferOutput<typeof ColumnSchema>;

export async function listColumns(fetcher: typeof fetch = fetch): Promise<ColumnInfo[]> {
	const r = await fetcher(apiUrl(`/api/columns${datasetSuffix()}`));
	return asJson(r, v.array(ColumnSchema));
}

// ── Descriptor + dataset discovery ──────────────────────────────────────────
import {
	DatasetDescriptorSchema,
	DatasetsResponseSchema,
	DatasetView as DatasetViewClass,
} from './descriptor';

/** List the datasets the backend serves (id + table stats + capabilities). */
export async function listDatasets(fetcher: typeof fetch = fetch) {
	const r = await fetcher(apiUrl('/api/datasets'));
	return asJson(r, DatasetsResponseSchema).then((d) => d.datasets);
}

/** Fetch + parse one dataset's descriptor and wrap it in a DatasetView. */
export async function getDatasetView(
	datasetId: string,
	isDefault: boolean,
	fetcher: typeof fetch = fetch,
): Promise<DatasetView> {
	const r = await fetcher(apiUrl(`/api/datasets/${encodeURIComponent(datasetId)}/descriptor`));
	if (!r.ok) throw await apiErrorFrom(r);
	// Parse directly (not via asJson) so the value keeps the schema's OUTPUT type,
	// where `.default()`-ed fields are required — the DatasetView ctor's shape.
	const descriptor = v.parse(DatasetDescriptorSchema, await r.json());
	return new DatasetViewClass(descriptor).withDatasetParam(isDefault);
}

// ── Row-media URL helpers (identity arity from the active view) ──────────────
export const thumbnailUrl = (row: Row): string => activeView().thumbnailUrl(row);
export const chunkFrameUrl = (row: Row): string => activeView().frameUrl(row);
export const mediaUrl = (row: Row): string => activeView().mediaUrl(row);

// ── Embedding Atlas ───────────────────────────────────────────────────────
/** The projection spaces the atlas can render — names come from the descriptor
 *  (`declared.atlas[].name`), so this is `string`, not a fixed corpus set. */
export type AtlasSpace = string;

const AtlasStatusSchema = v.object({
	projected: v.boolean(),
	rows: v.pipe(v.number(), v.integer()),
	space: v.optional(v.string()),
	// Which named spaces are built (gates the space toggle).
	spaces: v.optional(v.record(v.string(), v.boolean())),
});
export type AtlasStatus = v.InferOutput<typeof AtlasStatusSchema>;

export async function getAtlasStatus(
	space: AtlasSpace,
	fetcher: typeof fetch = fetch,
): Promise<AtlasStatus> {
	const ds = activeView().datasetParam();
	const q = ds ? `&dataset=${encodeURIComponent(ds)}` : '';
	return asJson(
		await fetcher(apiUrl(`/api/atlas/status?space=${encodeURIComponent(space)}${q}`)),
		AtlasStatusSchema,
	);
}

/** A factorized (codes, labels) pair from one Arrow DICTIONARY column. */
export interface DictColumn {
	codes: Int32Array;
	labels: string[];
}

/** Decoded point arrays for a space. Identity keys beyond the doc key and the
 *  categorical colour channels are keyed by name (descriptor-driven), so no
 *  corpus column appears in the type. */
export interface AtlasPoints {
	count: number;
	space?: string;
	x: Float32Array;
	y: Float32Array;
	xBits: Uint16Array;
	yBits: Uint16Array;
	docs: string[];
	doc: Int32Array;
	docFiles?: string[];
	rowid?: number[];
	cluster?: Int32Array;
	/** Non-doc identity key columns (e.g. the 2nd/3rd key field) by field name. */
	keys: Record<string, number[]>;
	/** Categorical colour channels (language/topic/…) by channel name. */
	channels: Record<string, DictColumn>;
}

function dictColumn(vec: Vector | null): DictColumn | null {
	if (!vec) return null;
	let labels: string[] = [];
	for (const d of vec.data) {
		if (d.dictionary && labels.length === 0) labels = d.dictionary.toArray() as string[];
	}
	return { codes: concatInt32(vec), labels };
}

function concatInt32(vec: Vector): Int32Array {
	const chunks = vec.data;
	if (chunks.length === 1) return chunks[0]!.values as Int32Array;
	let total = 0;
	for (const d of chunks) total += d.length;
	const out = new Int32Array(total);
	let off = 0;
	for (const d of chunks) {
		out.set((d.values as Int32Array).subarray(0, d.length), off);
		off += d.length;
	}
	return out;
}

function int32Column(vec: Vector | null): Int32Array {
	return (vec?.toArray() ?? new Int32Array()) as Int32Array;
}

function u16Column(vec: Vector | null): Uint16Array {
	return (vec?.toArray() ?? new Uint16Array()) as Uint16Array;
}

/** Decode raw float16 bits → an owned Float32Array (CPU math). */
function f16ToF32(bits: Uint16Array): Float32Array {
	const out = new Float32Array(bits.length);
	for (let i = 0; i < bits.length; i++) {
		const h = bits[i]!;
		const expo = (h & 0x7c00) >> 10;
		const sigf = (h & 0x03ff) / 1024;
		const sign = (h & 0x8000) === 0 ? 1 : -1;
		if (expo === 0x1f) out[i] = sign * (sigf ? Number.NaN : Infinity);
		else if (expo === 0x00) out[i] = sign * (sigf ? 6.103515625e-5 * sigf : 0);
		else out[i] = sign * 2 ** (expo - 15) * (1 + sigf);
	}
	return out;
}

function numberColumn(vec: Vector | null): number[] {
	if (!vec) return [];
	const out: number[] = [];
	for (const v of vec) out.push(Number(v));
	return out;
}

/** Fetch the point arrays for a space as one Apache Arrow IPC stream. Identity
 *  keys (minus the doc key) and the declared channels are read by name from the
 *  active view, so the reader names no corpus column. */
export async function getAtlasPoints(
	space: AtlasSpace,
	fetcher: typeof fetch = fetch,
): Promise<AtlasPoints> {
	const view = activeView();
	const ds = view.datasetParam();
	const dsq = ds ? `&dataset=${encodeURIComponent(ds)}` : '';
	const url = apiUrl(`/api/atlas/points?space=${encodeURIComponent(space)}&v=6${dsq}`);

	const load = async (): Promise<AtlasPoints> => {
		const r = await fetcher(url);
		if (!r.ok) throw await apiErrorFrom(r);
		return parseAtlasPoints(r, view, space);
	};

	// An injected fetcher means a test or a probe: never serve it a memo, or a stub returning 502 would be
	// handed a cached success and the test would assert nothing.
	return fetcher === fetch ? atlasMemo.run(url, load) : load();
}

/** Three projections: enough that toggling between spaces is free — the measured waste — without letting a
 *  session accumulate multi-megabyte payloads it will not look at again. `&v=6` in the URL is the schema
 *  token, so bumping it invalidates every entry at once and no eviction message is needed. */
const atlasMemo = new UrlMemo<AtlasPoints>(3);

async function parseAtlasPoints(
	r: Response,
	view: ReturnType<typeof activeView>,
	space: AtlasSpace,
): Promise<AtlasPoints> {
	const buf = await r.arrayBuffer();
	const table = tableFromIPC(new Uint8Array(buf));
	const md = table.schema.metadata;

	const count = Number(md.get('count') ?? table.numRows);
	const spaceMeta = md.get('space');
	const docFilesMeta = md.get('docFiles');

	const doc = dictColumn(table.getChild('doc'));
	if (!doc) throw new ApiError(500, 'malformed /api/atlas/points payload (no doc column)');

	const xBits = u16Column(table.getChild('x'));
	const yBits = u16Column(table.getChild('y'));

	// Non-doc identity key columns, by field name.
	const keys: Record<string, number[]> = {};
	for (const field of view.keyFields) {
		if (field === view.docKeyField) continue;
		const col = table.getChild(field);
		if (col) keys[field] = numberColumn(col);
	}

	// Declared categorical channels, by channel name.
	const channels: Record<string, DictColumn> = {};
	for (const name of view.atlasChannels(space)) {
		const col = dictColumn(table.getChild(name));
		if (col) channels[name] = col;
	}

	const data: AtlasPoints = {
		count,
		docs: doc.labels,
		doc: doc.codes,
		xBits,
		yBits,
		x: f16ToF32(xBits),
		y: f16ToF32(yBits),
		keys,
		channels,
	};
	if (spaceMeta) data.space = spaceMeta;
	if (docFilesMeta) data.docFiles = v.parse(v.array(v.string()), JSON.parse(docFilesMeta));
	const rowid = table.getChild('rowid');
	if (rowid) data.rowid = numberColumn(rowid);
	const cluster = table.getChild('cluster');
	if (cluster) data.cluster = int32Column(cluster);

	if (!Number.isFinite(data.count) || data.x.length !== data.count) {
		throw new ApiError(500, 'malformed /api/atlas/points payload');
	}
	return data;
}

/** Full row for one point (detail pane + playback), looked up by identity keys. */
export async function getAtlasChunk(
	keys: (string | number)[],
	fetcher: typeof fetch = fetch,
): Promise<Row> {
	const path = keys.map((k) => encodeURIComponent(String(k))).join('/');
	const r = await fetcher(apiUrl(`/api/atlas/chunk/${path}${datasetSuffix()}`));
	return asJson(r, RowSchema);
}

/** Full rows for a selection, addressed by stable Lance `_rowid`. */
export async function getAtlasChunks(
	rowids: number[],
	fetcher: typeof fetch = fetch,
): Promise<Row[]> {
	const ds = activeView().datasetParam();
	const url = ds ? `/api/atlas/chunks?dataset=${encodeURIComponent(ds)}` : '/api/atlas/chunks';
	const r = await fetcher(apiUrl(url), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ rowids }),
	});
	return asJson(r, HitsArraySchema);
}

// ── Topics (Tree page, capability-gated) ────────────────────────────────────
export interface TopicNode {
	name: string;
	value?: number | undefined;
	children?: TopicNode[] | undefined;
}

const TopicNodeSchema: v.GenericSchema<TopicNode> = v.lazy(() =>
	v.object({
		name: v.string(),
		value: v.optional(v.number()),
		children: v.optional(v.array(TopicNodeSchema)),
	}),
);

const TopicsResponseSchema = v.object({
	built: v.boolean(),
	layers: v.pipe(v.number(), v.integer()),
	n_chunks: v.pipe(v.number(), v.integer()),
	hierarchy: v.nullable(TopicNodeSchema),
	noise_label: v.optional(v.string()),
});
export type TopicsResponse = v.InferOutput<typeof TopicsResponseSchema>;

export async function getTopics(fetcher: typeof fetch = fetch): Promise<TopicsResponse> {
	return asJson(await fetcher(apiUrl(`/api/topics${datasetSuffix()}`)), TopicsResponseSchema);
}

// ── Knowledge graph (Graph page, capability-gated) ──────────────────────────
const ENTITY_ID_RE = /^[0-9a-f]{16}$/;

export function isEntityId(id: string): boolean {
	return ENTITY_ID_RE.test(id);
}

export const GraphStatusSchema = v.object({
	built: v.boolean(),
	entities: v.pipe(v.number(), v.integer()),
	relations: v.pipe(v.number(), v.integer()),
	mentions: v.pipe(v.number(), v.integer()),
	videos: v.pipe(v.number(), v.integer()),
});
export type GraphStatus = v.InferOutput<typeof GraphStatusSchema>;

export async function getGraphStatus(fetcher: typeof fetch = fetch): Promise<GraphStatus> {
	return asJson(await fetcher(apiUrl(`/api/graph/status${datasetSuffix()}`)), GraphStatusSchema);
}

const CypherValueSchema = v.union([v.string(), v.number(), v.null_()]);
export type CypherValue = v.InferOutput<typeof CypherValueSchema>;

export const GraphCypherResponseSchema = v.object({
	built: v.boolean(),
	columns: v.array(v.string()),
	rows: v.array(v.array(CypherValueSchema)),
	error: v.nullable(v.string()),
});
export type GraphCypherResponse = v.InferOutput<typeof GraphCypherResponseSchema>;

export async function runGraphCypher(
	query: string,
	limit = 200,
	fetcher: typeof fetch = fetch,
): Promise<GraphCypherResponse> {
	const ds = activeView().datasetParam();
	const url = ds ? `/api/graph/cypher?dataset=${encodeURIComponent(ds)}` : '/api/graph/cypher';
	const r = await fetcher(apiUrl(url), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ query, limit }),
	});
	return asJson(r, GraphCypherResponseSchema);
}

export const GraphMatchSchema = v.object({
	entity_id: v.string(),
	name: v.string(),
	entity_type: v.string(),
	mention_count: v.pipe(v.number(), v.integer()),
	videos: v.pipe(v.number(), v.integer()),
});
export type GraphMatch = v.InferOutput<typeof GraphMatchSchema>;

const GraphSearchResponseSchema = v.object({
	built: v.boolean(),
	matches: v.array(GraphMatchSchema),
});
export type GraphSearchResponse = v.InferOutput<typeof GraphSearchResponseSchema>;

export async function searchGraphEntities(
	q: string,
	fetcher: typeof fetch = fetch,
): Promise<GraphSearchResponse> {
	const ds = activeView().datasetParam();
	const dsq = ds ? `&dataset=${encodeURIComponent(ds)}` : '';
	return asJson(
		await fetcher(apiUrl(`/api/graph/search?q=${encodeURIComponent(q)}${dsq}`)),
		GraphSearchResponseSchema,
	);
}

const GraphEntitySchema = v.object({
	entity_id: v.string(),
	name: v.string(),
	entity_type: v.string(),
	mention_count: v.pipe(v.number(), v.integer()),
});
export type GraphEntity = v.InferOutput<typeof GraphEntitySchema>;

// A clip carries its doc id + time span + body text plus a display title; the
// title's source column is the dataset's (graph_presets.clip_title_column).
const GraphClipSchema = v.object({
	chunk_id: v.string(),
	doc_id: v.string(),
	title: v.string(),
	start: v.number(),
	end: v.number(),
	text: v.string(),
});
export type GraphClip = v.InferOutput<typeof GraphClipSchema>;

const GraphNeighborSchema = v.object({
	entity_id: v.string(),
	name: v.string(),
	entity_type: v.string(),
	direction: v.picklist(['out', 'in']),
	description: v.string(),
});
export type GraphNeighbor = v.InferOutput<typeof GraphNeighborSchema>;

const GraphCooccurSchema = v.object({
	entity_id: v.string(),
	name: v.string(),
	shared: v.pipe(v.number(), v.integer()),
});
export type GraphCooccur = v.InferOutput<typeof GraphCooccurSchema>;

export const GraphEntityResponseSchema = v.object({
	built: v.boolean(),
	entity: v.nullable(GraphEntitySchema),
	clips: v.array(GraphClipSchema),
	neighbors: v.array(GraphNeighborSchema),
	cooccur: v.array(GraphCooccurSchema),
});
export type GraphEntityResponse = v.InferOutput<typeof GraphEntityResponseSchema>;

export async function getGraphEntity(
	entityId: string,
	fetcher: typeof fetch = fetch,
): Promise<GraphEntityResponse> {
	if (!isEntityId(entityId)) throw new ApiError(400, `invalid entity id: ${entityId}`);
	return asJson(
		await fetcher(apiUrl(`/api/graph/entity/${encodeURIComponent(entityId)}${datasetSuffix()}`)),
		GraphEntityResponseSchema,
	);
}

const GraphNodeSchema = v.object({
	id: v.string(),
	name: v.string(),
	type: v.string(),
	mentions: v.pipe(v.number(), v.integer()),
	videos: v.pipe(v.number(), v.integer()),
});
export type GraphNode = v.InferOutput<typeof GraphNodeSchema>;

const GraphEdgeSchema = v.object({
	source: v.string(),
	target: v.string(),
	description: v.string(),
});
export type GraphEdge = v.InferOutput<typeof GraphEdgeSchema>;

export const GraphSubgraphResponseSchema = v.object({
	built: v.boolean(),
	nodes: v.array(GraphNodeSchema),
	edges: v.array(GraphEdgeSchema),
});
export type GraphSubgraphResponse = v.InferOutput<typeof GraphSubgraphResponseSchema>;

export async function getGraphSubgraph(
	entityId?: string,
	limit = 150,
	fetcher: typeof fetch = fetch,
): Promise<GraphSubgraphResponse> {
	const params = new URLSearchParams({ limit: String(limit) });
	if (entityId !== undefined) {
		if (!isEntityId(entityId)) throw new ApiError(400, `invalid entity id: ${entityId}`);
		params.set('entity_id', entityId);
	}
	const ds = activeView().datasetParam();
	if (ds) params.set('dataset', ds);
	return asJson(
		await fetcher(apiUrl(`/api/graph/subgraph?${params}`)),
		GraphSubgraphResponseSchema,
	);
}

// ── Voice search ("Find this voice", capability-gated) ──────────────────────
const VoiceStatusSchema = v.object({
	built: v.boolean(),
	turns: v.pipe(v.number(), v.integer()),
	speakers: v.pipe(v.number(), v.integer()),
});
export type VoiceStatus = v.InferOutput<typeof VoiceStatusSchema>;

export async function getVoiceStatus(fetcher: typeof fetch = fetch): Promise<VoiceStatus> {
	return asJson(await fetcher(apiUrl(`/api/voice/status${datasetSuffix()}`)), VoiceStatusSchema);
}

/** A voice-ranked hit: the matched row (renders as a normal result card) plus
 *  the matched speaker turn. The row envelope is a {@link Row}; the turn fields
 *  are the voice capability's own contract. */
export const VoiceHitSchema = v.intersect([
	RowSchema,
	v.object({
		speaker_label: v.string(),
		turn_id: v.pipe(v.number(), v.integer()),
		turn_start: v.number(),
		turn_end: v.number(),
		_distance: v.number(),
		turn_score: v.number(),
	}),
]) as v.GenericSchema<VoiceHit>;
export type VoiceHit = Row & {
	speaker_label: string;
	turn_id: number;
	turn_start: number;
	turn_end: number;
	_distance: number;
	turn_score: number;
};

const VoiceQueryInfoSchema = v.object({
	doc_id: v.nullable(v.string()),
	speaker_label: v.nullable(v.string()),
	turn_id: v.nullable(v.pipe(v.number(), v.integer())),
	turn_start: v.nullable(v.number()),
	turn_end: v.nullable(v.number()),
});
export type VoiceQueryInfo = v.InferOutput<typeof VoiceQueryInfoSchema>;

export const VoiceSimilarResponseSchema = v.object({
	query: VoiceQueryInfoSchema,
	hits: v.array(VoiceHitSchema),
});
export type VoiceSimilarResponse = v.InferOutput<typeof VoiceSimilarResponseSchema>;

/** Query-by-example anchor: a document plus exactly one locator. */
export type VoiceAnchor = { docId: string } & (
	| { turnId: number }
	| { speaker: string }
	| { t: number }
);

export async function voiceSimilar(
	anchor: VoiceAnchor,
	opts: { n?: number | undefined; excludeSameDoc?: boolean | undefined } = {},
	fetcher: typeof fetch = fetch,
): Promise<VoiceSimilarResponse> {
	const params = new URLSearchParams({ doc_id: anchor.docId });
	if ('turnId' in anchor) params.set('turn_id', String(anchor.turnId));
	else if ('speaker' in anchor) params.set('speaker', anchor.speaker);
	else params.set('t', String(anchor.t));
	if (opts.n !== undefined) params.set('n', String(opts.n));
	if (opts.excludeSameDoc !== undefined)
		params.set('exclude_same_doc', String(opts.excludeSameDoc));
	const ds = activeView().datasetParam();
	if (ds) params.set('dataset', ds);
	return asJson(await fetcher(apiUrl(`/api/voice/similar?${params}`)), VoiceSimilarResponseSchema);
}

export async function voiceSimilarUpload(
	file: File,
	opts: { n?: number | undefined } = {},
	fetcher: typeof fetch = fetch,
): Promise<VoiceSimilarResponse> {
	const params = new URLSearchParams();
	if (opts.n !== undefined) params.set('n', String(opts.n));
	const ds = activeView().datasetParam();
	if (ds) params.set('dataset', ds);
	const fd = new FormData();
	fd.append('file', file);
	const url = params.size > 0 ? `/api/voice/similar?${params}` : '/api/voice/similar';
	const r = await fetcher(apiUrl(url), { method: 'POST', body: fd });
	return asJson(r, VoiceSimilarResponseSchema);
}

export type VoiceBand = 'strong' | 'possible';

export function voiceBandOf(turnScore: number): VoiceBand | null {
	if (turnScore >= 0.7) return 'strong';
	if (turnScore >= 0.6) return 'possible';
	return null;
}

/** Narrow a Row to a VoiceHit so shared result components render the speaker
 *  chip only for voice-mode hits. Structural check (validated at fetch). */
export function isVoiceHit(hit: Row): hit is VoiceHit {
	return 'turn_score' in hit && 'speaker_label' in hit;
}
