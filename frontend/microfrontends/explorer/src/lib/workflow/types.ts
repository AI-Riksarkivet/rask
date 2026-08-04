/**
 * Shared vocabulary for the workflow editor — the node kinds + the per-node
 * config/runtime shapes. Kept in its own module so the graph store AND the Zod
 * persistence schema can both import them without an import cycle (the graph
 * re-exports the public ones, so components keep importing from graph.svelte).
 */
import type { Hit, SearchMode, SearchSpec } from '@rask/explorer-api';

/** The pipeline stages, as a runtime list (drives the persistence schema). */
export const NODE_KINDS = [
	'query',
	'image',
	'filter',
	'atlas',
	'search',
	'combine',
	'tagger',
	'results',
	'export',
] as const;
/** A node's `type` (used to pick its component) is its kind. */
export type NodeKind = (typeof NODE_KINDS)[number];

/** Runtime guard for xyflow's `Node.type: string | undefined`. */
export function isNodeKind(v: string | undefined): v is NodeKind {
	return v !== undefined && (NODE_KINDS as readonly string[]).includes(v);
}

export type RunStatus = 'idle' | 'running' | 'done' | 'error';

/** Head size the cross-encoder reranker re-scores when a Search has rerank on.
 *  One source of truth so the executor spec and the Inspector badge can't drift. */
export const RERANK_TOP_N = 20;

/** The Search node's two target ports: general upstream (query / filter /
 *  results scope) vs the image input. Shared by the SearchNode handles, the
 *  connection validation, and the persistence migration. */
export const SEARCH_IN_HANDLE = 'in';
export const SEARCH_IMAGE_HANDLE = 'image';

/** A Search node's result count (`n`): default + allowed range. One source of
 *  truth for the default config, the persistence clamp, and the SearchNode input. */
export const DEFAULT_N = 24;
export const MIN_N = 1;
export const MAX_N = 100;

/** How a Combine node merges its incoming result sets. */
export type CombineMode = 'union' | 'intersect';

/** Granularity of a Search's refinement by an upstream result set. */
export type RefineScope = 'video' | 'chunk';

/** Per-node user input. One flat shape covers every kind (each node UI only
 *  edits the fields it cares about) — simpler than a discriminated union for a
 *  POC, and the unused fields stay at their harmless defaults. */
export interface NodeConfig {
	/** query node, and the Search node's own inline query. */
	q: string;
	/** image: the uploaded file fed to the visual leg (POST multipart). Never
	 *  persisted (a File can't serialise) — `imageName` survives a reload so the
	 *  Image node can prompt for re-upload. */
	image: File | null;
	imageName: string;
	/** filter: a raw SQL WHERE plus structured facets keyed by descriptor
	 *  filterable field name (matches SearchSpec.filters). */
	where: string;
	filters: Record<string, string>;
	/** search: how the merged inputs are ranked. */
	mode: SearchMode;
	n: number;
	rerank: boolean;
	/** search: drop hits whose normalized relevance (`relevanceOf`, higher =
	 *  better) is below this. `null` = off; hits with no ranking signal pass. */
	minScore: number | null;
	/** search: when scoped by an upstream result set, narrow to those documents
	 *  (doc-key `IN`, may return new rows) or to the exact upstream rows (the
	 *  descriptor's identity-key `IN`, result ⊆ upstream). */
	refineScope: RefineScope;
	/** combine: union vs intersect of incoming result sets. */
	combineMode: CombineMode;
	/** tagger: tags this node stamps onto every hit that flows through it. */
	tags: string[];
	/** export: download format + which columns to include. `null` = every column
	 *  the active dataset offers (resolved from the descriptor at export time). */
	exportFormat: 'json' | 'csv';
	exportColumns: string[] | null;
	/** atlas: the exact hit set the user captured in the Atlas modal viewer
	 *  (lasso/box/legend selection). `null` until a selection is confirmed; the
	 *  executor emits this downstream (NOT the live global crossFilter). Not
	 *  persisted (see persistence.ts) — a reload discards the capture. */
	capturedAtlasSelection: Hit[] | null;
	/** ergonomics: optional custom title; disabled nodes are bypassed on run. */
	label: string;
	enabled: boolean;
}

/** Per-node RUN state (status / hits / error / timing), keyed by node id. */
export interface NodeRuntime {
	status: RunStatus;
	error: string | null;
	/** Result hits (Search / Combine / Results nodes). */
	hits: Hit[] | null;
	/** Result count summary (cheap to render without holding the array). */
	count: number | null;
	/** Last run wall-clock in ms (Search node). */
	ms: number | null;
	/** How many videos this Search was scoped to by upstream results (null = whole corpus). */
	scopedDocs: number | null;
	/** How many exact chunks a chunk-level refine scoped to (null = not chunk-scoped). */
	scopedChunks: number | null;
	/** True when the scope hit its cap (`MAX_SCOPE_DOCS` / `MAX_SCOPE_CHUNKS`) and was truncated. */
	scopeCapped: boolean;
	/** Count of upstream inputs ignored because only one query/image is used. */
	droppedInputs: number;
	/** Last output this node produced (spec + hits) — reused as the upstream
	 *  input when a downstream node is run individually, so "play" on one node
	 *  doesn't force the whole branch to re-execute. */
	output: NodeOutput | null;
	/** Fingerprint of the config + incoming edges the output was computed from
	 *  (see WorkflowGraph.nodeFingerprint). A mismatch at reuse time means the
	 *  node was edited or rewired since — the cache must not be served. */
	outputKey: string | null;
	/** True when an upstream node re-ran after this node's last run — its shown
	 *  results no longer reflect the graph. Cleared when the node runs again. */
	stale: boolean;
}

/** What travels along an edge from a node to its successors. */
export interface NodeOutput {
	/** Partial search spec contributed by this node (query text, image, filters). */
	spec: Partial<SearchSpec>;
	/** A concrete result set, once a Search/Combine node has produced one. */
	hits: Hit[] | null;
	/** True when this node errored (or was blocked by an upstream error) — the
	 *  executor blocks every dependent instead of running it on partial input. */
	failed?: boolean;
}

/** A detached node, copied to the clipboard for paste. */
export interface ClipboardNode {
	type: NodeKind;
	config: NodeConfig;
	position: { x: number; y: number };
}
