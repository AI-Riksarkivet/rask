/**
 * The graph execution engine — a parallel dataflow over the node graph, kept
 * separate from the store so the algorithm is decoupled from (and testable
 * without) WorkflowGraph's internals. It reads topology + config and writes
 * results back through the narrow `RunDeps` seam; it never touches the class.
 */
import type { Edge, Node } from '@xyflow/svelte';
import { relevanceOf, search, type Hit, type SearchSpec } from '@rask/media-api';
import { hitKey } from '$lib/utils';
import { chunkScopeClause, dedupeHits, videoScopeClause } from './scope';
import {
	RERANK_TOP_N,
	type NodeConfig,
	type NodeKind,
	type NodeOutput,
	type NodeRuntime,
} from './types';

/** Everything the executor needs from the graph — a narrow interface so the
 *  store stays in control of state while the algorithm lives here. */
export interface RunDeps {
	nodes: Node[];
	edges: Edge[];
	/** Resolved config for a node (the store fills in defaults for unknowns). */
	config(id: string): NodeConfig;
	kindOf(id: string): NodeKind | null;
	patchRuntime(id: string, patch: Partial<NodeRuntime>): void;
	/** Stamp a Tagger node's tags onto the passing hits (shared tag store). */
	tagHits(hits: Hit[], tags: string[]): void;
	/** A node's reusable last output for a partial run, or null when it must be
	 *  recomputed (never ran, failed, stale, or its config/wiring changed since
	 *  the output was recorded — compared via `fingerprint`). */
	cachedOutput(id: string): NodeOutput | null;
	/** Current fingerprint of a node's output-affecting config + incoming
	 *  edges; stored next to the cached output so reuse can detect edits. */
	fingerprint(id: string): string;
}

/**
 * Execute the graph as a dataflow: every node runs as soon as its OWN
 * predecessors resolve, so independent branches (e.g. two Search legs feeding a
 * Combine) run concurrently instead of strictly one-after-another. Promises are
 * seeded in topological order, so when a node reads its predecessors' promises
 * they already exist. A node failure never rejects (it records an error on that
 * node) but it BLOCKS everything downstream — running a dependent on partial
 * input would silently produce wrong results. Returns a cycle-error message to
 * surface, or null on success.
 */
export async function runGraph(deps: RunDeps): Promise<string | null> {
	const ids = deps.nodes.map((n) => n.id);
	const incoming = incomingMap(deps.edges, new Set(ids));

	const order = topoOrder(ids, incoming);
	if (!order) return CYCLE_ERROR;

	const outputs = new Map<string, Promise<NodeOutput>>();
	for (const id of order) {
		// `order` is topological, so every predecessor's promise is already set.
		outputs.set(id, computeNode(deps, id, incoming.get(id) ?? [], outputs));
	}
	await Promise.all(outputs.values());
	return null;
}

/**
 * Run ONE node (the per-node ▶ button): its upstream closure executes too, but
 * a predecessor with a reusable cached output (see `RunDeps.cachedOutput`) is
 * read instead of re-run — so rerunning B in A→B→C re-executes only B against
 * A's existing results, and the first run of B computes A once. The target
 * itself ALWAYS recomputes; `fresh` forces the whole upstream branch to as
 * well. Nodes outside the closure are untouched (C keeps its results — the
 * caller flags them stale). Returns the ids actually executed.
 */
export async function runSubgraph(
	deps: RunDeps,
	targetId: string,
	opts: { fresh?: boolean } = {},
): Promise<{ error: string | null; ran: string[] }> {
	const ids = new Set(deps.nodes.map((n) => n.id));
	if (!ids.has(targetId)) return { error: null, ran: [] };
	const incoming = incomingMap(deps.edges, ids);

	// Upstream closure: the target plus everything it (transitively) reads.
	const closure = new Set<string>([targetId]);
	const stack = [targetId];
	while (stack.length) {
		const id = stack.pop()!;
		for (const p of incoming.get(id) ?? []) {
			if (!closure.has(p)) {
				closure.add(p);
				stack.push(p);
			}
		}
	}

	const order = topoOrder([...closure], incoming);
	if (!order) return { error: CYCLE_ERROR, ran: [] };

	const outputs = new Map<string, Promise<NodeOutput>>();
	const ran: string[] = [];
	const ranSet = new Set<string>();
	for (const id of order) {
		const preds = incoming.get(id) ?? [];
		// A node whose input is being recomputed THIS run can't serve its cache —
		// it was computed from the predecessor's previous output. `order` is
		// topological, so every pred's run/reuse decision is already made.
		const predRecomputed = preds.some((p) => ranSet.has(p));
		const cached = id === targetId || opts.fresh || predRecomputed ? null : deps.cachedOutput(id);
		if (cached) {
			outputs.set(id, Promise.resolve(cached));
			continue;
		}
		ran.push(id);
		ranSet.add(id);
		outputs.set(id, computeNode(deps, id, preds, outputs));
	}
	await Promise.all(outputs.values());
	return { error: null, ran };
}

const CYCLE_ERROR = 'The graph has a cycle — remove a connection and run again.';

/** target → [sources], restricted to `ids` (drops edges touching unknown nodes). */
function incomingMap(edges: Edge[], ids: Set<string>): Map<string, string[]> {
	const incoming = new Map<string, string[]>([...ids].map((id) => [id, []]));
	for (const e of edges) {
		if (incoming.has(e.target) && ids.has(e.source)) incoming.get(e.target)!.push(e.source);
	}
	return incoming;
}

/** Execute a node once its predecessors' promises resolve, caching its output
 *  on the runtime (and clearing any stale flag) for later partial runs. */
function computeNode(
	deps: RunDeps,
	id: string,
	preds: string[],
	outputs: Map<string, Promise<NodeOutput>>,
): Promise<NodeOutput> {
	return Promise.all(preds.map((p) => outputs.get(p)!)).then(async (predOutputs) => {
		// Fingerprint BEFORE executing: an edit landing mid-run must not stamp an
		// output computed from the old config as fresh under the new key. (An
		// edit after this line records under the OLD key → mismatch → the cache
		// is simply not reused — the safe direction.)
		const key = deps.fingerprint(id);
		const out = await runNode(deps, id, predOutputs);
		deps.patchRuntime(id, { output: out, outputKey: key, stale: false });
		return out;
	});
}

/** Topologically order `ids` by the `incoming` edges among them; returns null
 *  if they contain a cycle. */
function topoOrder(ids: string[], incoming: Map<string, string[]>): string[] | null {
	const members = new Set(ids);
	const deg = new Map<string, number>(
		ids.map((id) => [id, (incoming.get(id) ?? []).filter((p) => members.has(p)).length]),
	);
	const outgoing = new Map<string, string[]>();
	for (const id of ids) {
		for (const p of incoming.get(id) ?? []) {
			if (!members.has(p)) continue;
			const list = outgoing.get(p);
			if (list) list.push(id);
			else outgoing.set(p, [id]);
		}
	}
	const queue = ids.filter((id) => (deg.get(id) ?? 0) === 0);
	const order: string[] = [];
	while (queue.length) {
		const id = queue.shift()!;
		order.push(id);
		for (const t of outgoing.get(id) ?? []) {
			const d = (deg.get(t) ?? 0) - 1;
			deg.set(t, d);
			if (d === 0) queue.push(t);
		}
	}
	return order.length === ids.length ? order : null;
}

/** Run one node: merge its predecessors' outputs into its input, then execute by
 *  kind. Returns the output that travels to successors. Isolated — any throw is
 *  recorded on the node and surfaces as a failed empty output, so it never
 *  rejects a dependent's `Promise.all`; the `failed` flag blocks dependents. */
async function runNode(deps: RunDeps, id: string, predOutputs: NodeOutput[]): Promise<NodeOutput> {
	const kind = deps.kindOf(id);
	// kindOf is null only for an unknown/corrupt node id — nothing to run. Handled
	// here so the switch below stays exhaustive over NodeKind.
	if (kind === null) return { spec: {}, hits: null };
	const cfg = deps.config(id);

	// An upstream failure blocks this node (and, via the flag, everything after
	// it) — even through a disabled node, which only bypasses its OWN work.
	if (predOutputs.some((o) => o.failed)) {
		deps.patchRuntime(id, { status: 'error', error: 'Skipped — an upstream node failed.' });
		return { spec: {}, hits: null, failed: true };
	}

	// Merge upstream outputs. `inSpec` = WHAT to search; `scope` = the union of
	// upstream result sets (WHERE). Track per-source hit sets (Combine·intersect)
	// and same-field collisions (honesty badges).
	const inSpec: Partial<SearchSpec> = {};
	const scopeHits: Hit[] = [];
	const sourceHitSets: Hit[][] = [];
	let qContrib = 0;
	let imgContrib = 0;
	for (const o of predOutputs) {
		if (o.spec.q) qContrib += 1;
		if (o.spec.image) imgContrib += 1;
		// Merge structured filters field-by-field so two Filter nodes don't clobber
		// each other (the rest of the spec keeps last-writer-wins, as before).
		const { filters, ...rest } = o.spec;
		Object.assign(inSpec, rest);
		if (filters) inSpec.filters = { ...inSpec.filters, ...filters };
		if (o.hits && o.hits.length) {
			scopeHits.push(...o.hits);
			sourceHitSets.push(o.hits);
		}
	}
	const scope: Hit[] | null = scopeHits.length ? dedupeHits(scopeHits) : null;

	// Disabled node: bypass it — forward the scope, contribute nothing.
	if (!cfg.enabled) {
		deps.patchRuntime(id, { status: 'idle', hits: scope, count: scope?.length ?? null });
		return { spec: {}, hits: scope };
	}

	try {
		switch (kind) {
			case 'query': {
				const q = cfg.q.trim();
				deps.patchRuntime(id, { status: q ? 'done' : 'idle' });
				return { spec: q ? { q } : {}, hits: null };
			}
			case 'image': {
				deps.patchRuntime(id, { status: cfg.image ? 'done' : 'idle' });
				return { spec: cfg.image ? { image: cfg.image } : {}, hits: null };
			}
			case 'filter': {
				const spec: Partial<SearchSpec> = {};
				if (cfg.where.trim()) spec.where = cfg.where.trim();
				// Structured facets keyed by descriptor filterable field name.
				const filters: Record<string, string> = {};
				for (const [field, value] of Object.entries(cfg.filters)) {
					const v = value.trim();
					if (v) filters[field] = v;
				}
				if (Object.keys(filters).length) spec.filters = filters;
				deps.patchRuntime(id, { status: Object.keys(spec).length ? 'done' : 'idle' });
				return { spec, hits: null };
			}
			case 'atlas': {
				// Emit the selection the user CAPTURED in the Atlas modal viewer
				// (stored on the node's config), NOT the live global crossFilter — so
				// the /atlas page and the workflow no longer share live map state. An
				// empty/null capture is idle (the node emits nothing downstream).
				const captured = cfg.capturedAtlasSelection;
				const hits = captured && captured.length ? captured : null;
				deps.patchRuntime(id, {
					status: hits ? 'done' : 'idle',
					hits,
					count: hits?.length ?? null,
				});
				return { spec: {}, hits };
			}
			case 'combine': {
				let combined: Hit[] = [];
				if (sourceHitSets.length) {
					if (cfg.combineMode === 'intersect') {
						const keySets = sourceHitSets.map((s) => new Set(s.map(hitKey)));
						combined = dedupeHits(
							sourceHitSets[0]!.filter((h) => keySets.every((ks) => ks.has(hitKey(h)))),
						);
					} else {
						combined = scope ?? [];
					}
				}
				deps.patchRuntime(id, {
					status: sourceHitSets.length ? 'done' : 'idle',
					hits: combined,
					count: combined.length,
				});
				return { spec: {}, hits: combined.length ? combined : null };
			}
			case 'tagger': {
				// Stamp this node's tags onto every passing chunk (shared store), then
				// forward them unchanged. Inline tags on the same chunks survive too.
				if (scope) deps.tagHits(scope, cfg.tags);
				deps.patchRuntime(id, {
					status: scope ? 'done' : 'idle',
					hits: scope,
					count: scope?.length ?? null,
				});
				return { spec: {}, hits: scope };
			}
			case 'search': {
				// Query is a connected Query node if wired, else this node's inline field.
				const q = inSpec.q?.trim() || cfg.q.trim();
				const image = inSpec.image ?? null;
				// Dropped wired inputs: extra duplicate upstreams, plus the inline query
				// when an upstream query also supplied one.
				const inlineQDropped = cfg.q.trim() && qContrib > 0 ? 1 : 0;
				const droppedInputs =
					Math.max(0, qContrib - 1) + Math.max(0, imgContrib - 1) + inlineQDropped;
				if (!q && !image) {
					// Nothing to search for — pass the scope through so a half-configured
					// node never breaks the chain.
					deps.patchRuntime(id, {
						status: 'idle',
						hits: scope,
						count: scope?.length ?? null,
						droppedInputs,
					});
					return { spec: {}, hits: scope };
				}
				deps.patchRuntime(id, { status: 'running' });

				const spec: SearchSpec = { q, n: cfg.n, mode: cfg.mode };
				if (cfg.rerank) {
					spec.rerank = true;
					spec.rerankN = RERANK_TOP_N;
				}
				if (image) spec.image = image;
				if (inSpec.filters) spec.filters = inSpec.filters;

				// WHERE: any upstream filter, ANDed with the refinement scope — either
				// the upstream videos (`doc_id IN`) or the exact upstream chunks.
				const wheres: string[] = [];
				if (inSpec.where) wheres.push(inSpec.where);
				let scopedDocs: number | null = null;
				let scopedChunks: number | null = null;
				let scopeCapped = false;
				if (scope?.length) {
					const sc =
						cfg.refineScope === 'chunk' ? chunkScopeClause(scope) : videoScopeClause(scope);
					if (sc) {
						wheres.push(sc.clause);
						scopeCapped = sc.capped;
						if (cfg.refineScope === 'chunk') scopedChunks = sc.count;
						else scopedDocs = sc.count;
					}
				}
				// Parenthesize each clause: a user filter like `a = 1 OR b = 2` must not
				// let OR-precedence swallow the ANDed scope clause.
				if (wheres.length) spec.where = wheres.map((w) => `(${w})`).join(' AND ');

				const t0 = performance.now();
				let hits = await search(spec);
				const ms = Math.round(performance.now() - t0);
				// Threshold: drop hits below the configured relevance. Hits with no
				// ranking signal (e.g. scene browsing) pass — there's nothing to compare.
				if (cfg.minScore != null) {
					const min = cfg.minScore;
					hits = hits.filter((h) => {
						const r = relevanceOf(h);
						return r === null || r >= min;
					});
				}
				deps.patchRuntime(id, {
					status: 'done',
					hits,
					count: hits.length,
					ms,
					scopedDocs,
					scopedChunks,
					scopeCapped,
					droppedInputs,
				});
				return { spec: {}, hits };
			}
			// Sinks: collect the incoming hits and surface them (Results renders them;
			// Export downloads them). Neither contributes a spec.
			case 'results':
			case 'export': {
				deps.patchRuntime(id, {
					status: scope ? 'done' : 'idle',
					hits: scope,
					count: scope?.length ?? null,
				});
				return { spec: {}, hits: scope };
			}
			default: {
				// Compile-time exhaustiveness: adding a NodeKind without a case errors here.
				const _exhaustive: never = kind;
				return _exhaustive;
			}
		}
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		deps.patchRuntime(id, { status: 'error', error: msg });
		return { spec: {}, hits: null, failed: true };
	}
}
