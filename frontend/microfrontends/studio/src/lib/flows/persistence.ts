/**
 * The localStorage boundary for the flow graph — valibot schemas that parse
 * (don't validate) persisted JSON back into typed values, self-healing bad/old
 * data via `v.fallback`. `safeParseGraph` is the one entry point, shared by the
 * localStorage load AND undo/redo restore.
 *
 * localStorage is the v0 store (key below); the follow-up is the catalog's
 * per-subject user-state document, as recorded in open_studio_flows.md.
 */
import * as v from 'valibot';
import { BODY_MODES, DEFAULT_JSON_BODY, DEFAULT_PROMPT, NODE_KINDS, REGEX_MODES } from './types';

export const STORAGE_KEY = 'studio-flow-graph-v1';

/** Per-node config as stored (no image File — `fileName` survives so the node
 *  can prompt for re-upload). Every field self-heals to a sane default. */
const ConfigSchema = v.object({
	label: v.fallback(v.string(), ''),
	enabled: v.fallback(v.boolean(), true),
	fileName: v.fallback(v.string(), ''),
	text: v.fallback(v.string(), ''),
	app: v.fallback(v.string(), 'htrflow'),
	requestName: v.fallback(v.string(), ''),
	modelPath: v.fallback(v.string(), ''),
	bodyMode: v.fallback(v.picklist(BODY_MODES), 'upstream'),
	bodyTemplate: v.fallback(v.string(), DEFAULT_JSON_BODY),
	promptTemplate: v.fallback(v.string(), DEFAULT_PROMPT),
	datasetTable: v.fallback(v.string(), ''),
	mcpServer: v.fallback(v.string(), ''),
	mcpTool: v.fallback(v.string(), ''),
	mcpArgs: v.fallback(v.string(), '{}'),
	extractPath: v.fallback(v.string(), ''),
	regexPattern: v.fallback(v.string(), ''),
	regexReplace: v.fallback(v.string(), ''),
	regexMode: v.fallback(v.picklist(REGEX_MODES), 'extract'),
});

/** A structurally-bad node (unknown kind, missing position) fails the whole
 *  parse, so the caller falls back to `seed()` instead of crashing the canvas. */
const PersistedNodeSchema = v.object({
	id: v.string(),
	type: v.picklist(NODE_KINDS),
	position: v.object({ x: v.number(), y: v.number() }),
});

const PersistedEdgeSchema = v.object({
	id: v.string(),
	source: v.string(),
	target: v.string(),
});

const PersistedGraphSchema = v.object({
	nodes: v.pipe(v.array(PersistedNodeSchema), v.minLength(1)),
	edges: v.optional(v.array(PersistedEdgeSchema), []),
	config: v.optional(v.record(v.string(), ConfigSchema), {}),
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
