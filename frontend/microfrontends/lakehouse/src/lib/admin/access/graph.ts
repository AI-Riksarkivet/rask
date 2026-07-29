// The explorer's pure layer: turn tuples and derivation trees into a laid-out graph, and decide what
// the current query highlights. No Svelte, no fetch — so every rule here is unit-testable, which matters
// because "which nodes are lit" IS the answer the view exists to give.
//
// Layout is computed here rather than delegated to dagre. Not dogma: the three shapes this view draws
// are a TREE (a derivation), a star (one subject → many objects) and its inverse — none needs a general
// DAG layerer, and a layout that is a pure function of the data is one that a test can assert on.

import type { ExpandNode, Tuple } from '../access';

/** The three questions the query bar asks. Each is a different OpenFGA primitive, and they answer
 *  genuinely different things — which is why the bar offers all three rather than one "search". */
export type QueryKind = 'what' | 'who' | 'why';

export type ExplorerQuery =
	| { kind: 'what'; user: string; relation: string; type: string }
	| { kind: 'who'; object: string; relation: string }
	| { kind: 'why'; user: string; relation: string; object: string; depth: number };

/** Why a node is on screen. `focus` is the seed, `path` is on the resolving derivation, `dim` is
 *  context that the current query did not touch. Dimming rather than hiding is deliberate: an
 *  operator needs to see that the rest of the graph exists, or "filtered down" reads as "gone". */
export type NodeRole = 'focus' | 'path' | 'subject' | 'container' | 'dim';

export type GraphNode = {
	id: string;
	fgaType: string;
	label: string;
	role: NodeRole;
	/** Column in the layout — derivation depth for `why`, side for the others. */
	depth: number;
	/** The mechanism that put this node here, shown on the node so a hop is self-explaining. */
	via: string | null;
};

export type GraphEdge = {
	id: string;
	source: string;
	target: string;
	label: string;
	onPath: boolean;
};

export type BuiltGraph = {
	nodes: GraphNode[];
	edges: GraphEdge[];
	/** Every type present, with a count — the facet rail's left column. */
	typeCounts: Map<string, number>;
	/** Every relation present, with a count — the facet rail's right column. */
	relationCounts: Map<string, number>;
};

export const idType = (id: string): string => id.split(':')[0] || 'unknown';

export const idLabel = (id: string): string => {
	const withoutType = id.includes(':') ? id.slice(id.indexOf(':') + 1) : id;
	// A userset (`team:eng#member`) keeps its relation — dropping it would render a team and one of its
	// rungs as the same node, silently merging two different subjects.
	return withoutType;
};

/** Subjects (who) vs containers (what) — the model's own split: `user`/`team`/`role` are the only
 *  types that appear on the left of a tuple as principals. */
const SUBJECT_TYPES = new Set(['user', 'team', 'role']);

export const isSubject = (id: string): boolean => SUBJECT_TYPES.has(idType(id));

// --------------------------------------------------------------------------- //
// The derivation tree → nodes + edges
// --------------------------------------------------------------------------- //

/** The OpenFGA mechanism a hop used, in the model's own words. Naming it on the edge is what turns a
 *  picture into an explanation — a grant three hops away otherwise looks identical to a direct one. */
export type Mechanism = 'direct' | 'implied-by' | 'inherited-from' | 'excluded-by' | 'requires';

const MECHANISM_LABEL: Record<Mechanism, string> = {
	direct: 'granted directly',
	'implied-by': 'implied by',
	'inherited-from': 'inherited from',
	'excluded-by': 'EXCLUDED by',
	requires: 'also requires',
};

export const mechanismLabel = (m: Mechanism): string => MECHANISM_LABEL[m];

type Walked = {
	nodes: Map<string, GraphNode>;
	edges: GraphEdge[];
};

/**
 * Flatten a derivation tree into a graph, keeping the operator on every edge.
 *
 * `intersection` and `difference` are carried through as `requires` / `excluded-by` rather than
 * dropped. Both reference playgrounds discard them — the official Types Previewer draws neither, and
 * its tree builder falls through to a default case on both — which is exactly how a subject that the
 * model EXCLUDES renders as granted. That failure is the reason this function exists.
 */
export function walkDerivation(
	tree: ExpandNode | null,
	rootObject: string,
	rootRelation: string,
): BuiltGraph {
	const acc: Walked = { nodes: new Map(), edges: [] };
	const rootId = rootObject;
	acc.nodes.set(rootId, {
		id: rootId,
		fgaType: idType(rootId),
		label: idLabel(rootId),
		role: 'focus',
		depth: 0,
		via: rootRelation,
	});
	if (tree) visit(tree, rootId, rootRelation, 0, 'direct', acc);
	return finish(acc);
}

function nodeFor(id: string, depth: number, via: string, acc: Walked): void {
	const existing = acc.nodes.get(id);
	if (existing) {
		// Keep the SHALLOWEST depth: the same subject reached by two paths belongs at its nearest hop,
		// or the layout stretches to the longest explanation rather than the clearest one.
		if (depth < existing.depth) existing.depth = depth;
		return;
	}
	acc.nodes.set(id, {
		id,
		fgaType: idType(id),
		label: idLabel(id),
		// `path`, because everything a derivation tree contains IS the derivation. This is the role the
		// facet filter refuses to hide — hiding part of the answer while still calling it the answer is
		// the one thing this view must not do. Note the contrast with buildNeighbourhood below, whose
		// nodes are RESULTS and stay filterable: narrowing a result list is the point of the rail.
		role: 'path',
		depth,
		via,
	});
}

function link(source: string, target: string, label: string, acc: Walked): void {
	const id = `${source}->${target}:${label}`;
	if (acc.edges.some((e) => e.id === id)) return;
	acc.edges.push({ id, source, target, label, onPath: true });
}

function visit(
	node: ExpandNode,
	anchor: string,
	relation: string,
	depth: number,
	mechanism: Mechanism,
	acc: Walked,
): void {
	for (const child of node.union ?? []) visit(child, anchor, relation, depth, mechanism, acc);
	for (const child of node.intersection ?? [])
		visit(child, anchor, relation, depth, 'requires', acc);
	if (node.difference) {
		if (node.difference.base) visit(node.difference.base, anchor, relation, depth, mechanism, acc);
		if (node.difference.subtract) {
			visit(node.difference.subtract, anchor, relation, depth, 'excluded-by', acc);
		}
	}

	const leaf = node.leaf;
	if (!leaf) return;

	// Terminal subjects — the actual grantees, including a `user:*` public wildcard, which must never
	// be hidden: a wildcard is the single widest grant the model can express.
	for (const subject of leaf.users ?? []) {
		nodeFor(subject, depth + 1, mechanismLabel(mechanism), acc);
		link(subject, anchor, `${relation} · ${mechanismLabel(mechanism)}`, acc);
	}

	// A same-object rung: `reader` satisfied because the subject is `writer` here.
	if (leaf.computed) {
		for (const child of leaf.expanded ?? []) {
			visit(child, anchor, leaf.computed, depth, 'implied-by', acc);
		}
	}

	// `X from Y` — the hop into the parent object, where a concentric model's answer actually lives.
	if (leaf.tuple_to_userset) {
		for (const child of leaf.expanded ?? []) {
			const childObject = (child.name ?? '').split('#')[0] ?? '';
			const childRelation = (child.name ?? '').split('#')[1] ?? relation;
			if (!childObject) continue;
			nodeFor(childObject, depth + 1, mechanismLabel('inherited-from'), acc);
			link(childObject, anchor, `${relation} · ${mechanismLabel('inherited-from')}`, acc);
			visit(child, childObject, childRelation, depth + 1, 'direct', acc);
		}
	}
}

function finish(acc: Walked): BuiltGraph {
	const nodes = [...acc.nodes.values()];
	return { nodes, edges: acc.edges, ...countFacets(nodes, acc.edges) };
}

// --------------------------------------------------------------------------- //
// Tuples → the neighbourhood graph (what is on screen before any query)
// --------------------------------------------------------------------------- //

/** One hop of raw tuples around a seed. This is the "see ALL relationships" baseline the query then
 *  narrows — without it the canvas is empty until someone knows what to ask, which is backwards. */
export function buildNeighbourhood(seed: string, tuples: readonly Tuple[]): BuiltGraph {
	const nodes = new Map<string, GraphNode>();
	nodes.set(seed, {
		id: seed,
		fgaType: idType(seed),
		label: idLabel(seed),
		role: 'focus',
		depth: 0,
		via: null,
	});
	const edges: GraphEdge[] = [];
	for (const t of tuples) {
		for (const [id, depth] of [
			[t.user, -1],
			[t.object, 1],
		] as const) {
			if (id === seed || nodes.has(id)) continue;
			nodes.set(id, {
				id,
				fgaType: idType(id),
				label: idLabel(id),
				role: isSubject(id) ? 'subject' : 'container',
				depth,
				via: null,
			});
		}
		const id = `${t.user}->${t.object}:${t.relation}`;
		if (!edges.some((e) => e.id === id)) {
			edges.push({ id, source: t.user, target: t.object, label: t.relation, onPath: false });
		}
	}
	const list = [...nodes.values()];
	return { nodes: list, edges, ...countFacets(list, edges) };
}

function countFacets(
	nodes: readonly GraphNode[],
	edges: readonly GraphEdge[],
): { typeCounts: Map<string, number>; relationCounts: Map<string, number> } {
	const typeCounts = new Map<string, number>();
	for (const n of nodes) typeCounts.set(n.fgaType, (typeCounts.get(n.fgaType) ?? 0) + 1);
	const relationCounts = new Map<string, number>();
	for (const e of edges) {
		// The mechanism suffix is presentation; the facet is the RELATION, so a derivation edge and a
		// stored tuple for the same relation land in one bucket rather than two.
		const relation = e.label.split(' · ')[0] ?? e.label;
		relationCounts.set(relation, (relationCounts.get(relation) ?? 0) + 1);
	}
	return { typeCounts, relationCounts };
}

// --------------------------------------------------------------------------- //
// Merge, filter, lay out
// --------------------------------------------------------------------------- //

/** Overlay a query's answer on the neighbourhood: shared nodes take the answer's role, and anything
 *  the query did not touch is dimmed rather than removed. */
export function overlay(base: BuiltGraph, answer: BuiltGraph | null): BuiltGraph {
	if (!answer) return base;
	const merged = new Map<string, GraphNode>();
	for (const n of base.nodes) merged.set(n.id, { ...n, role: 'dim' });
	// Roles are taken VERBATIM. An earlier version promoted every answer node to `path` here, which
	// sounds harmless and is not: `applyFacets` never hides a `path` node, so promoting the results of
	// "what can alice reach" made the facet rail structurally incapable of narrowing them — the exact
	// thing it exists for. Only a DERIVATION is `path` (walkDerivation says so); results stay filterable.
	for (const n of answer.nodes) merged.set(n.id, { ...n });
	const edges = new Map<string, GraphEdge>();
	for (const e of base.edges) edges.set(e.id, { ...e, onPath: false });
	for (const e of answer.edges) edges.set(e.id, { ...e, onPath: true });
	const nodes = [...merged.values()];
	const edgeList = [...edges.values()];
	return { nodes, edges: edgeList, ...countFacets(nodes, edgeList) };
}

export type Facets = { types: ReadonlySet<string>; relations: ReadonlySet<string> };

/**
 * Narrow what renders. An empty facet set means "no filter", NOT "nothing" — the distinction is the
 * difference between a fresh view and a view that looks broken.
 *
 * A node on the resolving path is never filtered out. Hiding part of the answer while still calling it
 * the answer is the one thing this view must not do; the facets are for the surrounding context.
 */
export function applyFacets(graph: BuiltGraph, facets: Facets): BuiltGraph {
	const { types, relations } = facets;
	if (types.size === 0 && relations.size === 0) return graph;

	const keepNode = (n: GraphNode) =>
		n.role === 'focus' || n.role === 'path' || types.size === 0 || types.has(n.fgaType);
	const nodes = graph.nodes.filter(keepNode);
	const present = new Set(nodes.map((n) => n.id));
	const edges = graph.edges.filter((e) => {
		if (!present.has(e.source) || !present.has(e.target)) return false;
		if (e.onPath || relations.size === 0) return true;
		return relations.has(e.label.split(' · ')[0] ?? e.label);
	});
	// Counts stay those of the UNFILTERED graph so the rail keeps showing what turning a facet back on
	// would bring — a count that shrinks to match the filter can never be used to undo it.
	return { nodes, edges, typeCounts: graph.typeCounts, relationCounts: graph.relationCounts };
}

export type Positioned = GraphNode & { x: number; y: number };

const COLUMN_WIDTH = 260;
const ROW_HEIGHT = 78;

/** Column-per-depth, rows within a column. Deterministic, so the same answer lays out the same way
 *  twice — a graph that reshuffles on refresh cannot be compared against the one before it. */
export function layout(nodes: readonly GraphNode[]): Positioned[] {
	const byDepth = new Map<number, GraphNode[]>();
	for (const n of nodes) {
		const bucket = byDepth.get(n.depth);
		if (bucket) bucket.push(n);
		else byDepth.set(n.depth, [n]);
	}
	const depths = [...byDepth.keys()].sort((a, b) => a - b);
	const tallest = Math.max(1, ...[...byDepth.values()].map((b) => b.length));
	const out: Positioned[] = [];
	for (const [column, depth] of depths.entries()) {
		const bucket = (byDepth.get(depth) ?? []).toSorted((a, b) => a.id.localeCompare(b.id));
		const offset = ((tallest - bucket.length) * ROW_HEIGHT) / 2;
		for (const [row, node] of bucket.entries()) {
			out.push({ ...node, x: column * COLUMN_WIDTH, y: offset + row * ROW_HEIGHT });
		}
	}
	return out;
}
