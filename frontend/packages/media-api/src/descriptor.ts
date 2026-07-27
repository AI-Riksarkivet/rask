/**
 * Dataset descriptor — the schema-agnostic contract the whole UI renders from.
 *
 * The backend (`GET /api/datasets/{id}/descriptor`) merges *discovered* facts
 * (tables, column types, vector dims, indexes) with *declared* semantic roles
 * (identity, media binding, display, search wiring, atlas spaces,
 * capabilities). These zod schemas describe THAT envelope — never a corpus's
 * column names — so the same build renders any dataset.
 *
 * :class:`DatasetView` wraps a parsed descriptor with typed accessors
 * (`rowKey`, `title`, `body`, media URLs, search modes, …). Components read
 * rows through it instead of hardcoding field names.
 */

import * as v from 'valibot';

import { apiUrl } from './base';

// ─────────────────────────────────────────────────────────────────────
// Descriptor envelope (mirrors services/common/lancekit/descriptor.py)
// ─────────────────────────────────────────────────────────────────────

const int = () => v.pipe(v.number(), v.integer());

const ColumnInfoSchema = v.object({
	name: v.string(),
	arrow_type: v.string(),
	nullable: v.boolean(),
	vector_dim: v.optional(v.nullable(int()), null),
	is_blob: v.optional(v.boolean(), false),
});
export type ColumnInfo = v.InferOutput<typeof ColumnInfoSchema>;

const TableInfoSchema = v.object({
	name: v.string(),
	row_count: int(),
	version: int(),
	columns: v.array(ColumnInfoSchema),
	indexes: v.array(
		v.object({ name: v.string(), index_type: v.string(), columns: v.array(v.string()) }),
	),
});
export type TableInfo = v.InferOutput<typeof TableInfoSchema>;

const IdentitySchema = v.object({
	key_fields: v.pipe(v.array(v.string()), v.minLength(1)),
	doc_key: v.optional(v.string(), 'doc_id'),
	doc_key_pattern: v.optional(v.string(), '.*'),
});

const DocumentBindingSchema = v.object({
	table: v.string(),
	media_blob: v.string(),
	mime: v.optional(v.nullable(v.string()), null),
	thumbnail: v.optional(v.nullable(v.string()), null),
	thumbnail_mime: v.optional(v.nullable(v.string()), null),
});

const TimeBindingSchema = v.object({ start: v.string(), end: v.string() });

const MetadataFieldSchema = v.object({ field: v.string(), label: v.string() });

const DisplaySchema = v.object({
	title: v.optional(v.array(v.string()), []),
	body: v.optional(v.nullable(v.string()), null),
	caption: v.optional(v.nullable(v.string()), null),
	metadata: v.optional(v.array(MetadataFieldSchema), []),
});

const VectorBindingSchema = v.object({
	table: v.string(),
	column: v.string(),
	dim: int(),
	query_encoder: v.string(),
	caption_source: v.optional(v.nullable(v.string()), null),
});

const SearchSchema = v.object({
	row_table: v.string(),
	fts: v.optional(
		v.nullable(
			v.object({
				table: v.string(),
				column: v.string(),
				language: v.optional(v.string(), 'English'),
			}),
		),
		null,
	),
	vectors: v.optional(v.record(v.string(), VectorBindingSchema), {}),
	filterable: v.optional(v.array(v.string()), []),
	rerank: v.optional(v.boolean(), false),
});

const AtlasChannelSchema = v.object({
	name: v.string(),
	column: v.optional(v.nullable(v.string()), null),
	broadest_prefix: v.optional(v.nullable(v.string()), null),
});

const AtlasSpaceSchema = v.object({
	name: v.string(),
	x: v.string(),
	y: v.string(),
	cluster: v.string(),
	source_column: v.string(),
	table: v.string(),
	channels: v.optional(v.array(AtlasChannelSchema), []),
});
export type AtlasSpaceDecl = v.InferOutput<typeof AtlasSpaceSchema>;

const DeclaredSchema = v.looseObject({
	identity: IdentitySchema,
	document: v.optional(v.nullable(DocumentBindingSchema), null),
	time: v.optional(v.nullable(TimeBindingSchema), null),
	display: v.optional(DisplaySchema, {}),
	search: v.optional(v.nullable(SearchSchema), null),
	atlas: v.optional(v.array(AtlasSpaceSchema), []),
	capabilities: v.optional(v.record(v.string(), v.string()), {}),
});

export const DatasetDescriptorSchema = v.object({
	id: v.string(),
	tables: v.record(v.string(), TableInfoSchema),
	declared: DeclaredSchema,
});
export type DatasetDescriptor = v.InferOutput<typeof DatasetDescriptorSchema>;

export const DatasetSummarySchema = v.object({
	id: v.string(),
	tables: v.optional(v.record(v.string(), v.object({ row_count: int(), version: int() })), {}),
	capabilities: v.optional(v.array(v.string()), []),
});
export const DatasetsResponseSchema = v.object({ datasets: v.array(DatasetSummarySchema) });

// ─────────────────────────────────────────────────────────────────────
// Row envelope — the ranking + alignments contract, NOT corpus columns
// ─────────────────────────────────────────────────────────────────────

const WordSchema = v.object({
	text: v.string(),
	start: v.number(),
	end: v.number(),
	score: v.optional(v.number()),
});
export type Word = v.InferOutput<typeof WordSchema>;

export const AlignmentSchema = v.object({
	start: v.number(),
	end: v.number(),
	text: v.string(),
	duration: v.optional(v.number()),
	score: v.optional(v.number()),
	words: v.optional(v.array(WordSchema)),
});
export type Alignment = v.InferOutput<typeof AlignmentSchema>;

/** A search/browse result row. The parse validates the ENVELOPE — ranking
 *  signals + the alignments blob — and passes every other (corpus-specific)
 *  column through untouched, so the schema names no corpus column. Typed
 *  field access goes through {@link DatasetView}. */
export const RowSchema = v.looseObject({
	_score: v.optional(v.number()),
	_distance: v.optional(v.number()),
	_relevance_score: v.optional(v.number()),
	alignments: v.optional(v.array(AlignmentSchema)),
	tags: v.optional(v.array(v.string())),
});
export type Row = v.InferOutput<typeof RowSchema> & Record<string, unknown>;

/** The search modes a dataset supports, derived from its declared bindings.
 *  The named literals are the corpus's current roles; the `(string & {})` arm
 *  keeps the type open so a dataset can declare a vector space under ANY key
 *  (e.g. `audio`) and have it flow through as a mode without a type edit. */
export type SearchMode =
	| 'fts'
	| 'semantic'
	| 'visual'
	| 'scene'
	| 'scene_fts'
	| 'hybrid'
	| 'all'
	| (string & {});

/** Generic column categories, derived from the LANCE/Arrow type (never a corpus
 *  role): an embedding is any fixed-size-list<float>, so text / pixel / audio /
 *  any-future embedding are ONE category. Drives type-appropriate UI (filters,
 *  search) without naming what a column means. */
export type ColumnCategory =
	| 'embedding'
	| 'blob'
	| 'numerical'
	| 'categorical'
	| 'temporal'
	| 'text'
	| 'other';

// `half_?float` catches both pyarrow's `halffloat` (float16) and a `half_float`
// spelling; the `\b` keeps `int`/`float` from matching inside longer words.
const _NUMERIC_RE = /\b(u?int\d*|float\d*|double|decimal\d*|half_?float)\b/;
const _TEMPORAL_RE = /\b(timestamp|date\d*|time\d*|duration|interval)\b/;

/** Classify a discovered column by its Lance/Arrow type. `ftsColumns` are the
 *  string columns that carry an FTS index (→ `text`, i.e. full-text searchable);
 *  every other string/bool is `categorical`. */
export function categoryOf(col: ColumnInfo, ftsColumns?: ReadonlySet<string>): ColumnCategory {
	if (col.vector_dim != null) return 'embedding'; // fixed_size_list<float> — uniform, any modality
	if (col.is_blob) return 'blob'; // lance.blob.v2 — media bytes
	const t = col.arrow_type.toLowerCase();
	if (ftsColumns?.has(col.name) && (t.includes('string') || t.includes('utf8'))) return 'text';
	// A dictionary type names an integer INDEX type (`dictionary<…, indices=int32>`),
	// so it must be classified before the numeric test or it'd read as 'numerical'.
	if (t.includes('dictionary')) return 'categorical';
	if (_NUMERIC_RE.test(t)) return 'numerical';
	if (_TEMPORAL_RE.test(t)) return 'temporal';
	if (t.includes('string') || t.includes('utf8') || t.includes('bool')) return 'categorical';
	return 'other';
}

/** One declared embedding space, treated uniformly regardless of what it embeds.
 *  `onRowTable` is the ONLY behavioural distinction (direct search vs a frame
 *  table ranked-then-joined) — derived from the binding, not the role name. */
export interface VectorSpace {
	key: string;
	table: string;
	column: string;
	dim: number;
	encoder: string;
	captionSource: string | null;
	onRowTable: boolean;
}

const COSINE_DISTANCE_MAX = 2;

// ─────────────────────────────────────────────────────────────────────
// DatasetView — typed accessors over a parsed descriptor
// ─────────────────────────────────────────────────────────────────────

function str(value: unknown): string | null {
	return value === null || value === undefined ? null : String(value);
}

function num(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export class DatasetView {
	constructor(readonly descriptor: DatasetDescriptor) {}

	get id(): string {
		return this.descriptor.id;
	}

	private get declared() {
		return this.descriptor.declared;
	}

	// ── identity ──────────────────────────────────────────────────────
	get keyFields(): string[] {
		return this.declared.identity.key_fields;
	}

	get docKeyField(): string {
		return this.declared.identity.doc_key;
	}

	/** Stable per-row identity key (the descriptor's key fields joined). */
	rowKey(row: Row): string {
		return this.keyFields.map((k) => String(row[k] ?? '')).join('|');
	}

	docId(row: Row): string {
		return String(row[this.docKeyField] ?? '');
	}

	/** The path segments a media route takes (doc key first, then the rest). */
	keyPath(row: Row): string[] {
		return this.keyFields.map((k) => encodeURIComponent(String(row[k] ?? '')));
	}

	// ── display ───────────────────────────────────────────────────────
	/** First non-empty declared title field, else the doc id. */
	title(row: Row): string {
		for (const field of this.declared.display.title) {
			const value = str(row[field]);
			if (value) return value;
		}
		return this.docId(row);
	}

	get bodyField(): string | null {
		return this.declared.display.body;
	}

	body(row: Row): string {
		return this.bodyField ? (str(row[this.bodyField]) ?? '') : '';
	}

	get captionField(): string | null {
		return this.declared.display.caption;
	}

	caption(row: Row): string | null {
		return this.captionField ? str(row[this.captionField]) : null;
	}

	/** Declared metadata fields (field + human label) for cards / meta panels. */
	get metadataFields(): { field: string; label: string }[] {
		return this.declared.display.metadata;
	}

	/** Resolved metadata rows for one hit (absent/empty values dropped). */
	metadata(row: Row): { field: string; label: string; value: string }[] {
		const out: { field: string; label: string; value: string }[] = [];
		for (const { field, label } of this.metadataFields) {
			const value = str(row[field]);
			if (value) out.push({ field, label, value });
		}
		return out;
	}

	// ── time ──────────────────────────────────────────────────────────
	get hasTime(): boolean {
		return this.declared.time !== null;
	}

	time(row: Row): { start: number; end: number } | null {
		const t = this.declared.time;
		if (!t) return null;
		const start = num(row[t.start]);
		const end = num(row[t.end]);
		return start === null || end === null ? null : { start, end };
	}

	duration(row: Row): number | null {
		const d = num(row['duration']);
		if (d !== null) return d;
		const t = this.time(row);
		return t ? t.end - t.start : null;
	}

	// ── search ────────────────────────────────────────────────────────
	get filterFields(): string[] {
		return this.declared.search?.filterable ?? [];
	}

	get hasFts(): boolean {
		return this.declared.search?.fts != null;
	}

	get rerankable(): boolean {
		return this.declared.search?.rerank ?? false;
	}

	/** String columns that carry an FTS index — the `text` (full-text) category. */
	private get ftsColumns(): ReadonlySet<string> {
		const fts = this.declared.search?.fts;
		return new Set(fts ? [fts.column] : []);
	}

	/** Category of one declared column, from its Lance type (null if unknown). */
	columnCategory(table: string, column: string): ColumnCategory | null {
		const info = this.descriptor.tables[table]?.columns.find((c) => c.name === column);
		return info ? categoryOf(info, this.ftsColumns) : null;
	}

	/** Every declared embedding space, uniform — text/pixel/audio all alike. The
	 *  only per-space behaviour (direct vs frame-join) rides on `onRowTable`. */
	get vectorSpaces(): VectorSpace[] {
		const search = this.declared.search;
		if (!search) return [];
		return Object.entries(search.vectors).map(([key, b]) => ({
			key,
			table: b.table,
			column: b.column,
			dim: b.dim,
			encoder: b.query_encoder,
			captionSource: b.caption_source,
			onRowTable: b.table === search.row_table,
		}));
	}

	/** The modes the search bar should offer, derived GENERICALLY from the
	 *  declared bindings: fts + one mode per embedding space (by its own key,
	 *  whatever it is) + a `_fts` variant for spaces with a caption source +
	 *  hybrid/all composites. A dataset that declares a new embedding key gets a
	 *  new mode with no code change here. */
	get searchModes(): SearchMode[] {
		const search = this.declared.search;
		if (!search) return [];
		// Only spaces the search box can actually drive: text- or image-encoder
		// spaces. A space with a foreign query encoder (e.g. an audio voiceprint) is
		// served by its own capability, not this box, and the backend would 400 a
		// text query against it — so it's not offered as a mode.
		const queryable = this.vectorSpaces.filter(
			(s) => s.encoder === 'text' || s.encoder === 'image',
		);
		const modes: SearchMode[] = [];
		if (search.fts != null) modes.push('fts');
		for (const s of queryable) modes.push(s.key);
		for (const s of queryable) if (s.captionSource) modes.push(`${s.key}_fts`);
		// Hybrid fuses FTS + a text vector on ONE table, so it needs a text-encoder
		// space on the row table; offer it only when that exists.
		const hasRowTextSpace = queryable.some((s) => s.encoder === 'text' && s.onRowTable);
		if (search.fts != null && hasRowTextSpace) modes.push('hybrid');
		if (modes.length > 1) modes.push('all');
		return modes;
	}

	hasMode(mode: SearchMode): boolean {
		return this.searchModes.includes(mode);
	}

	/** One comparable relevance number (higher = better), or null if unranked. */
	relevanceOf(row: Row, mode?: SearchMode): number | null {
		switch (mode) {
			case 'fts':
			case 'scene_fts':
				return row._score ?? null;
			case 'semantic':
			case 'visual':
				return row._distance != null ? COSINE_DISTANCE_MAX - row._distance : null;
			case 'hybrid':
				return row._relevance_score ?? null;
			case 'scene':
				return null;
			default:
				if (row._relevance_score != null) return row._relevance_score;
				if (row._score != null) return row._score;
				if (row._distance != null) return COSINE_DISTANCE_MAX - row._distance;
				return null;
		}
	}

	// ── media ─────────────────────────────────────────────────────────
	get hasMedia(): boolean {
		return this.declared.document !== null;
	}

	get hasThumbnail(): boolean {
		return this.declared.document?.thumbnail != null;
	}

	mediaUrl(row: Row): string {
		return apiUrl(`/api/media/${encodeURIComponent(this.docId(row))}${this.datasetQuery('?')}`);
	}

	thumbnailUrl(row: Row): string {
		return apiUrl(`/api/thumbnail/${encodeURIComponent(this.docId(row))}${this.datasetQuery('?')}`);
	}

	/** Per-row frame image (route arity = identity key fields). */
	frameUrl(row: Row): string {
		return apiUrl(`/api/chunk-frame/${this.keyPath(row).join('/')}${this.datasetQuery('?')}`);
	}

	// ── capabilities ──────────────────────────────────────────────────
	hasCapability(name: string): boolean {
		return name in this.declared.capabilities;
	}

	// ── atlas ─────────────────────────────────────────────────────────
	get atlasSpaces(): AtlasSpaceDecl[] {
		return this.declared.atlas;
	}

	atlasSpace(name: string): AtlasSpaceDecl | null {
		return this.atlasSpaces.find((s) => s.name === name) ?? null;
	}

	/** The categorical channel output-names an atlas space ships (for legends). */
	atlasChannels(name: string): string[] {
		return this.atlasSpace(name)?.channels.map((c) => c.name) ?? [];
	}

	// ── dataset selector (non-default datasets ride a `?dataset=` param) ──
	private isDefault = true;

	/** Mark this view as a non-default dataset so its URLs carry `?dataset=`. */
	withDatasetParam(isDefault: boolean): this {
		this.isDefault = isDefault;
		return this;
	}

	private datasetQuery(prefix: '?' | '&'): string {
		return this.isDefault ? '' : `${prefix}dataset=${encodeURIComponent(this.id)}`;
	}

	datasetParam(): string | null {
		return this.isDefault ? null : this.id;
	}
}

// ─────────────────────────────────────────────────────────────────────
// Active view — the app sets this once after loading the default dataset,
// so pure helpers (hitKey, media URLs) stay descriptor-driven without
// threading a view through every caller.
// ─────────────────────────────────────────────────────────────────────

let _active: DatasetView | null = null;

export function setActiveView(view: DatasetView): void {
	_active = view;
}

export function activeViewOrNull(): DatasetView | null {
	return _active;
}

/** The active dataset view. Throws if the descriptor hasn't loaded yet — call
 *  sites run after the app-root descriptor fetch resolves. */
export function activeView(): DatasetView {
	if (_active === null) throw new Error('dataset descriptor not loaded');
	return _active;
}
