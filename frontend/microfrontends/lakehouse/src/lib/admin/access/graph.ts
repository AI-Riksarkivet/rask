// The explorer's pure layer: turn tuples and derivation trees into a laid-out graph, and decide what
// the current query highlights. No Svelte, no fetch — so every rule here is unit-testable, which matters
// because "which nodes are lit" IS the answer the view exists to give.
//
// Layout is computed here rather than delegated to dagre. Not dogma: the three shapes this view draws
// are a TREE (a derivation), a star (one subject → many objects) and its inverse — none needs a general
// DAG layerer, and a layout that is a pure function of the data is one that a test can assert on.

import type { ExpandNode } from '../access';
// The zone's own layered placer — see `layout()` below for why this is reused rather than replaced
// by a layout library.
import { layout as placeLayered } from '@rask/flow';

/** The three questions the query bar asks. Each is a different OpenFGA primitive, and they answer
 *  genuinely different things — which is why the bar offers all three rather than one "search". */
export type QueryKind = 'what' | 'who' | 'why';

/** Why a node is on screen. `focus` is the seed, `path` is on the resolving derivation, `dim` is
 *  context that the current query did not touch. Dimming rather than hiding is deliberate: an
 *  operator needs to see that the rest of the graph exists, or "filtered down" reads as "gone". */
export type NodeRole = 'focus' | 'path' | 'subject' | 'container' | 'dim';

export type GraphNode = {
	id: string;
	fgaType: string;
	label: string;
	role: NodeRole;
	/**
	 * Layout column, SIGNED, and the sign is the convention the whole file shares: **negative is
	 * towards the subject, positive is towards the object, 0 is the focus.**
	 *
	 * It matters because it fixes reading order. A tuple reads `user:alice --owner--> table:x`, so the
	 * subject belongs on the left and the arrows run left-to-right. An earlier version numbered a
	 * derivation's hops positively, which put the focus OBJECT leftmost and made the whole chain read
	 * backwards against its own arrowheads — while `buildNeighbourhood` right next to it already used
	 * the signed form. One convention, both builders.
	 */
	depth: number;
	/** The mechanism that put this node here, shown on the node so a hop is self-explaining. */
	via: string | null;
};

/** What KIND of fact an edge is — the canvas colour-codes these, because on a dense store the three
 *  read very differently: a `parent` edge is scaffolding, a role/team edge is indirection, and only
 *  the rest are grants somebody chose to write. */
export type EdgeKind = 'grant' | 'membership' | 'structural';

export type GraphEdge = {
	id: string;
	source: string;
	target: string;
	label: string;
	onPath: boolean;
	/** Absent = uncoded (the model view keeps its single-colour schema look). */
	kind?: EdgeKind;
	/** The grant behind this edge is CONDITIONAL — time-boxed, and therefore not what it looks like.
	 *  An expiring grant drawn identically to a permanent one is the whole failure this feature exists
	 *  to prevent, so it has to reach the canvas, not just the tuple list. */
	condition?: string | null;
};

/** A tuple subject may be a USERSET — `role:validators#assignee`, "everyone assigned the role". The
 *  canvas draws that as the ROLE's own node (one card per role, never one per rung) with the rung
 *  kept as the node's `via` — a separate `validators#assignee` card beside `validators` drew the
 *  same thing twice and read as a bug. */
const subjectRef = (id: string): { node: string; rung: string | null } => {
	const hash = id.indexOf('#');
	return hash === -1 ? { node: id, rung: null } : { node: id.slice(0, hash), rung: id.slice(hash) };
};

const edgeKind = (subject: string, label: string): EdgeKind => {
	if (label === 'parent') return 'structural';
	const type = idType(subject);
	if (subject.includes('#') || type === 'role' || type === 'team') return 'membership';
	return 'grant';
};

/** The minimum a graph builder reads off a tuple.
 *
 *  Looser than `Tuple` on purpose: `list-objects` / `list-users` answers are SYNTHESISED edges, not
 *  stored tuples, and forcing them to invent a `condition` field would be a lie typed as a convenience. */
export type TupleLike = {
	user: string;
	relation: string;
	object: string;
	condition?: { name: string } | null;
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

const idLabel = (id: string): string => {
	const withoutType = id.includes(':') ? id.slice(id.indexOf(':') + 1) : id;
	// A userset (`team:eng#member`) keeps its relation — dropping it would render a team and one of its
	// rungs as the same node, silently merging two different subjects.
	return withoutType;
};

/** Subjects (who) vs containers (what) — the model's own split: `user`/`team`/`role` are the only
 *  types that appear on the left of a tuple as principals. */
const SUBJECT_TYPES = new Set(['user', 'team', 'role']);

export const isSubject = (id: string): boolean => SUBJECT_TYPES.has(idType(id));

/** The relation half of a userset reference. Expand answers `"table:db1$t#reader"`, not `"reader"`,
 *  and the qualified form used as a label repeats the object on an edge that already connects it.
 *  A bare relation passes through — the shape is not guaranteed across OpenFGA versions. */
const relationOf = (userset: string): string => {
	const hash = userset.lastIndexOf('#');
	return hash === -1 ? userset : userset.slice(hash + 1) || userset;
};

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

const mechanismLabel = (m: Mechanism): string => MECHANISM_LABEL[m];

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
		// Keep the depth NEAREST the focus: the same subject reached by two paths belongs at its shortest
		// hop, or the layout stretches to the longest explanation rather than the clearest one. Derivation
		// depths run negative (see GraphNode.depth), so "nearest" is the GREATER value, not the lesser.
		if (depth > existing.depth) existing.depth = depth;
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
		// Usersets fold into their role/team node here exactly as on the store canvas — see subjectRef.
		const ref = subjectRef(subject);
		nodeFor(ref.node, depth - 1, mechanismLabel(mechanism), acc);
		// The edge carries the RELATION only. The mechanism is already on the node this edge arrives at
		// (`data.via` → "NAMESPACE · INHERITED FROM"), so repeating it here said the same thing twice and
		// roughly doubled every label's width — Svelte Flow centres edge labels with no collision
		// avoidance, so "can_read_data · inherited from" sat on top of the node boxes and of each other.
		// One fact, one place: relation on the edge, mechanism on the node it explains.
		link(ref.node, anchor, relation, acc);
	}

	// A same-object rung: `reader` satisfied because the subject is `writer` here.
	if (leaf.computed) {
		// OpenFGA answers `computed` QUALIFIED — "table:bronze$pages#writer", not "writer". Used raw it
		// becomes the edge label, and the canvas reads "table:bronze$pages#writer · inherited from" on
		// an edge already drawn between those two nodes: the object repeated, the rung buried.
		const rung = relationOf(leaf.computed);
		for (const child of leaf.expanded ?? []) {
			visit(child, anchor, rung, depth, 'implied-by', acc);
		}
	}

	// `X from Y` — the hop into the parent object, where a concentric model's answer actually lives.
	if (leaf.tuple_to_userset) {
		for (const child of leaf.expanded ?? []) {
			const childObject = (child.name ?? '').split('#')[0] ?? '';
			const childRelation = (child.name ?? '').split('#')[1] ?? relation;
			if (!childObject) continue;
			nodeFor(childObject, depth - 1, mechanismLabel('inherited-from'), acc);
			link(childObject, anchor, relation, acc);
			visit(child, childObject, childRelation, depth - 1, 'direct', acc);
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

/**
 * The ENTIRE tuple store as one graph — the canvas's resting state.
 *
 * There is no seed and no focus: the store has no centre, so nothing is privileged until a query
 * names something. This is what "see all relationships, then filter down" actually requires, and its
 * absence was the view's central defect — the canvas opened empty and a query *added* a few nodes, so
 * the facet rail had nothing to narrow and the first thing anyone met was a blank box.
 *
 * Depth is assigned by ROLE rather than by hops, because a whole-store graph has no distance-from-
 * anything to measure: subjects (`user`/`team`/`role`) sit left, the containment spine
 * (warehouse → project → namespace) in the middle, and leaf resources (table, view, transaction) right.
 * That is the direction tuples already read in, so arrows run left-to-right here as everywhere else.
 */
export function buildWholeGraph(tuples: readonly TupleLike[]): BuiltGraph {
	// One column per RUNG of the hierarchy, so the canvas reads as the org chart it encodes:
	// users leftmost (people), then the groupings they belong to (team/role — membership edges point
	// rightward into them), then the containment chain root-first — project is the top of the tenancy
	// hierarchy, and every parent sits LEFT of its children so the parent→child edges all flow with
	// the reading direction. Before this, user/team/role shared one column and project shared
	// warehouse's, which drew a team beside its own members and the tenancy root mid-canvas.
	const SUBJECT_DEPTH: Record<string, number> = { user: -2, team: -1, role: -1 };
	const CONTAINER_DEPTH: Record<string, number> = {
		project: 0,
		warehouse: 1,
		namespace: 2,
		table: 3,
		materialized_view: 3,
		transaction: 3,
		model: 3,
	};
	const nodes = new Map<string, GraphNode>();
	const add = (id: string, rung: string | null = null) => {
		if (nodes.has(id)) return;
		const type = idType(id);
		nodes.set(id, {
			id,
			fgaType: type,
			label: idLabel(id),
			role: isSubject(id) ? 'subject' : 'container',
			depth: isSubject(id) ? (SUBJECT_DEPTH[type] ?? -1) : (CONTAINER_DEPTH[type] ?? 2),
			via: rung,
		});
	};
	const edges: GraphEdge[] = [];
	const seen = new Set<string>();
	for (const t of tuples) {
		const subject = subjectRef(t.user);
		add(subject.node, subject.rung);
		add(t.object);
		const id = `${subject.node}->${t.object}:${t.relation}`;
		if (seen.has(id)) continue;
		seen.add(id);
		edges.push({
			id,
			source: subject.node,
			target: t.object,
			label: t.relation,
			onPath: false,
			kind: edgeKind(t.user, t.relation),
			condition: t.condition?.name ?? null,
		});
	}
	const list = [...nodes.values()];
	return { nodes: list, edges, ...countFacets(list, edges) };
}

/** One hop of raw tuples around a seed — used for a query's own answer, where a centre does exist. */
export function buildNeighbourhood(seed: string, tuples: readonly TupleLike[]): BuiltGraph {
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
		const subject = subjectRef(t.user);
		for (const [id, depth, rung] of [
			[subject.node, -1, subject.rung],
			[t.object, 1, null],
		] as const) {
			if (id === seed || nodes.has(id)) continue;
			nodes.set(id, {
				id,
				fgaType: idType(id),
				label: idLabel(id),
				role: isSubject(id) ? 'subject' : 'container',
				depth,
				via: rung,
			});
		}
		const id = `${subject.node}->${t.object}:${t.relation}`;
		if (!edges.some((e) => e.id === id)) {
			edges.push({
				id,
				source: subject.node,
				target: t.object,
				label: t.relation,
				onPath: false,
				kind: edgeKind(t.user, t.relation),
				condition: t.condition?.name ?? null,
			});
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

/**
 * Place the graph, reusing the LINEAGE zone's layered placer.
 *
 * The first version here put each depth in a column and sorted rows alphabetically. That is layer
 * ASSIGNMENT with no ordering phase, so nothing tried to put connected nodes near one another and the
 * edges crossed arbitrarily — which is the whole of "the graph looks noisy". `$lib/lineage/layout`
 * already solves exactly this, with the barycentre crossing-minimisation sweep that was missing, and
 * it exists in this zone *because* dagre (1.1 MB) and elkjs (7.9 MB) do not fit its bundle budget.
 * Adding one of those here would have re-litigated a decision this zone already made and documented.
 *
 * Depth stays OURS: the lineage placer derives depth from edge direction, which is right for a
 * derivation DAG but wrong here — our depth is a semantic column (subjects left, containment spine,
 * resources right) that survives a graph with no edges at all. So we hand it `depthOf` and let it own
 * only the ordering.
 */
/**
 * Open the placer's gutters for THIS view's node size.
 *
 * The shared placer uses a fixed 240 px column / 110 px row, sized for the lineage zone's nodes. An
 * access node is up to 220 px wide, which leaves a 20 px gutter — and Svelte Flow centres every edge
 * label in exactly that gutter, with no collision avoidance. The result was `can_read_data` printed
 * across the node box next to it. Stretching the placed coordinates (rather than forking the placer)
 * keeps the barycentre ordering it computed and only changes the spacing it was never told about.
 */
// Kept SMALL on purpose. A big stretch does open the gutter, but it also makes the graph wider than the
// canvas (which is ~760 px here — rail + inspector take the rest), and `fitView` answers that by zooming
// OUT, so the labels stop colliding by becoming unreadable instead. The gutter is bought from the node's
// max-width instead (see AccessGraphNode), which costs nothing in graph width.
const COLUMN_STRETCH = 1.15;
const ROW_STRETCH = 1.1;

export function layout(
	nodes: readonly GraphNode[],
	edges: readonly GraphEdge[] = [],
): Positioned[] {
	if (nodes.length === 0) return [];
	// The placer's depths must start at 0 — it indexes `layers[d]` directly, and ours run negative.
	const minDepth = Math.min(...nodes.map((n) => n.depth));
	const depthOf = new Map(nodes.map((n) => [n.id, n.depth - minDepth]));
	const placed = placeLayered(
		nodes.map((n) => n.id),
		edges.map((e) => ({ source: e.source, target: e.target })),
		(id) => depthOf.get(id) ?? 0,
	);
	return nodes.map((n) => {
		const at = placed.get(n.id);
		return { ...n, x: (at?.x ?? 0) * COLUMN_STRETCH, y: (at?.y ?? 0) * ROW_STRETCH };
	});
}
