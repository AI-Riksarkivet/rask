/**
 * The localStorage boundary for the workflow graph — valibot schemas that parse
 * (don't validate) persisted JSON back into typed values, self-healing bad/old
 * data via `v.fallback`. Kept separate from the runtime store so the schema set
 * is easy to find and evolve. `safeParseGraph` is the one entry point.
 */
import * as v from 'valibot';
import type { SearchMode } from '@rask/media-api';
import { DEFAULT_N, MAX_N, MIN_N, NODE_KINDS } from './types';

// SearchMode is a plain union in the descriptor (no schema is exported), so
// mirror its members here to parse a persisted `mode`, healing anything unknown
// to 'fts'. `satisfies` keeps this list a subset of the SearchMode union.
const MODE_VALUES = [
	'fts',
	'semantic',
	'visual',
	'scene',
	'scene_fts',
	'hybrid',
	'all',
] as const satisfies readonly SearchMode[];
const SearchModeSchema = v.picklist(MODE_VALUES);

/** Per-node config as stored (no image File). Every field self-heals to a sane
 *  default on bad/old data via `v.fallback`, and `n` is clamped to [1, 100]. */
const ConfigSchema = v.object({
	q: v.fallback(v.string(), ''),
	imageName: v.fallback(v.string(), ''),
	where: v.fallback(v.string(), ''),
	filters: v.fallback(v.record(v.string(), v.string()), () => ({})),
	mode: v.fallback(SearchModeSchema, 'fts'),
	n: v.pipe(
		v.fallback(v.number(), DEFAULT_N),
		v.transform((n) => Math.max(MIN_N, Math.min(MAX_N, Math.round(n)))),
	),
	rerank: v.fallback(v.boolean(), false),
	minScore: v.fallback(v.nullable(v.number()), null),
	refineScope: v.fallback(v.picklist(['video', 'chunk']), 'video'),
	combineMode: v.fallback(v.picklist(['union', 'intersect']), 'union'),
	tags: v.fallback(v.array(v.string()), () => []),
	exportFormat: v.fallback(v.picklist(['json', 'csv']), 'csv'),
	// `null` = every column the active dataset offers; stale field names in a
	// persisted array self-heal at export time (orderColumns drops unknowns).
	exportColumns: v.fallback(v.nullable(v.array(v.string())), null),
	// Atlas modal capture is a full Row[] (media path, alignments, …) — too heavy
	// and stale-prone to round-trip through localStorage, so it is NOT persisted:
	// the schema always heals it to null, and a reload discards the capture (the
	// user re-opens the modal to re-select). TODO: persist a minimal identity-key
	// set + rehydrate via /api/atlas/chunks if needed.
	capturedAtlasSelection: v.fallback(v.null_(), null),
	label: v.fallback(v.string(), ''),
	enabled: v.fallback(v.boolean(), true),
});

// Nodes have fixed sizes (no NodeResizer) — old persisted width/height keys are
// unknown to this schema and valibot strips them, so stale data still parses.
const PersistedNodeSchema = v.object({
	id: v.string(),
	type: v.picklist(NODE_KINDS),
	position: v.object({ x: v.number(), y: v.number() }),
});

const PersistedEdgeSchema = v.object({
	id: v.string(),
	source: v.string(),
	target: v.string(),
	// Which target port the edge lands on (Search has "in" + "image"); absent in
	// pre-two-port snapshots — the loader infers it from the source kind.
	targetHandle: v.optional(v.string()),
	label: v.optional(v.string()),
});

/** A structurally-bad node (unknown kind, missing position) fails the whole
 *  parse, so the caller falls back to `seed()` instead of crashing the canvas. */
const PersistedGraphSchema = v.object({
	nodes: v.pipe(v.array(PersistedNodeSchema), v.minLength(1)),
	edges: v.optional(v.array(PersistedEdgeSchema), []),
	config: v.optional(v.record(v.string(), ConfigSchema), {}),
	tags: v.optional(v.record(v.string(), v.array(v.string())), {}),
});

export type PersistedConfig = v.InferOutput<typeof ConfigSchema>;
export type PersistedGraph = v.InferOutput<typeof PersistedGraphSchema>;

/** Parse a snapshot string into a typed graph, or null if absent/malformed. */
export function safeParseGraph(raw: string | null): PersistedGraph | null {
	if (!raw) return null;
	try {
		const result = v.safeParse(PersistedGraphSchema, JSON.parse(raw));
		return result.success ? result.output : null;
	} catch {
		return null; // not valid JSON
	}
}
