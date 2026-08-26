/**
 * Layered layout via elkjs — the coordinate-assignment phase the hand-rolled placer never had.
 *
 * `./layout.ts` implements the first three phases of Sugiyama (partition, longest-path layering,
 * barycentre ordering) and says so in its own docstring: *"it also does proper coordinate assignment
 * and routes long edges through dummy nodes, which this deliberately does not."* Those two omissions
 * are exactly what a reader sees as "clumped" and "not logical":
 *
 *  - **No coordinate assignment.** Every node in a layer is parked at a fixed `ROW_H` pitch in rank
 *    order, so a node is never pulled level with the neighbour it points at. Straight edges only
 *    happen by luck.
 *  - **No dummy nodes.** An edge spanning three layers is drawn as one straight line across whatever
 *    sits between, instead of being routed through reserved slots.
 *  - **Crude component packing.** Isolated nodes are bolted into a grid below the graph whose width
 *    is `layers.length`, so the more layers the graph has the wider that unrelated block gets.
 *
 * ELK does all three. It is also what MARQUEZ uses for the same picture — `web/libs/graph/src/layout/
 * useLayout.ts` runs `elk.algorithm: layered` with `elk.direction: RIGHT` — so this is convergence on
 * the reference implementation rather than a new idea.
 *
 * On edges, for accuracy: Marquez routes ORTHOGONALLY too and then RENDERS that route, drawing ELK's
 * bend points as an SVG polyline (`Edge/ElbowEdge.tsx`). An earlier revision of this file described
 * its routing as splines — that was simply wrong.
 *
 * rask now returns that geometry too. It used to ask ELK for the orthogonal route and read only
 * `x`/`y`, letting `smoothstep` redraw its own right angles — which mostly coincided, and quietly did
 * not wherever ELK had routed an edge AROUND something. `smoothstep` knows two endpoints and nothing
 * about the nodes between them, so a long edge crossing three layers was drawn straight through
 * whatever ELK had carefully steered it past. The bend points are the only record of that decision,
 * and throwing them away is throwing away the routing phase this file exists to gain.
 *
 * **Why this is not the dependency `layout.ts` rejected.** That file turned elk down on bundle size
 * "against ~7 KB of deferred-bundle headroom in this zone (`budget.json`)". `budget.json` no longer
 * exists — the per-zone ceiling and its `budget.test.ts` gate were removed 2026-08-04 — and `elkjs` is
 * ALREADY a dependency of the explorer zone, where `lib/workflow/layout.ts` has run it since the flow
 * editor shipped. The constraint that justified the hand-rolled placer is gone, and the library is
 * already vetted and installed.
 *
 * `layout()` stays: it is SYNCHRONOUS, and a caller that must place nodes inside a single tick (SSR,
 * a first paint before ELK resolves) still needs it. This is the async upgrade, not a replacement.
 */
import ELK from 'elkjs/lib/elk.bundled.js';
import type { ElkNode } from 'elkjs/lib/elk-api';

import type { LayoutEdge, Placed } from './layout';

/**
 * One lazily-built ELK instance.
 *
 * LAZY IS LOAD-BEARING, and the explorer's copy documents why: the constructor probes for `Worker`,
 * which does not exist server-side, so a module-scope `new ELK()` breaks SSR of every page that
 * imports this module — even a page that never lays anything out.
 */
let elk: InstanceType<typeof ELK> | null = null;

/** The medallion/job cards, when a node has not been measured yet. */
const DEFAULT_W = 200;
const DEFAULT_H = 64;

export interface ElkLayoutOptions {
	/** Reading direction. `RIGHT` is derivation order — upstream on the left. */
	direction?: 'RIGHT' | 'DOWN';
	/** Gap between ranks (columns in `RIGHT`). */
	layerGap?: number;
	/** Gap between siblings within a rank. */
	nodeGap?: number;
	/** Per-node box, when the caller knows better than the default card size. */
	size?: (id: string) => { width: number; height: number } | undefined;
	/**
	 * Group nodes into COMPOUND containers: return the container id a node belongs in, or
	 * `undefined` to leave it at the top level.
	 *
	 * ELK lays a container and its children out in ONE pass, so the containers are placed by the same
	 * layered algorithm as everything else rather than being drawn around a finished layout. That is
	 * what Marquez's column graph does — it builds a parent `kind: 'dataset'` node whose children are
	 * the columns (`column-level/layout.ts`) — and it is why `hierarchyHandling` is switched on below
	 * only when this hook is supplied: `INCLUDE_CHILDREN` exists for exactly this, and turning it on
	 * unconditionally would also disable component separation for flat graphs, which is the single
	 * biggest thing keeping an estate of mostly-unconnected nodes packed.
	 *
	 * CHILD COORDINATES COME BACK PARENT-RELATIVE, which is also what Svelte Flow's `parentId`
	 * expects — the two conventions agree, so nothing is converted.
	 */
	parentOf?: (id: string) => string | undefined;
	/** Inset between a container's border and its children. The top gets extra room for a label. */
	groupPadding?: number;
	/** Height reserved at the top of a container for its title. */
	groupLabelHeight?: number;
}

/** One point on an edge's route, in the same coordinate space as the node positions. */
export interface RoutePoint {
	x: number;
	y: number;
}

/**
 * ELK's computed route for one edge: where it decided the edge leaves, the corners it turns, and
 * where it arrives. `bendPoints` is empty for a straight edge, which is the common case and the
 * reason a renderer must have a no-bend path as well.
 */
export interface ElkRoute {
	start: RoutePoint;
	bendPoints: RoutePoint[];
	end: RoutePoint;
}

/** A compound container's placed box. */
export interface PlacedGroup extends Placed {
	width: number;
	height: number;
}

export interface ElkLayoutResult {
	/**
	 * Top-left coordinates keyed by node id — ABSOLUTE for a top-level node, PARENT-RELATIVE for one
	 * `parentOf` put inside a container, matching Svelte Flow's own `parentId` convention.
	 */
	nodes: Map<string, Placed>;
	/** Container boxes keyed by the id `parentOf` returned. Empty unless `parentOf` was supplied. */
	groups: Map<string, PlacedGroup>;
	/**
	 * Routes keyed `` `${source}>${target}` `` in the CALLER's derivation orientation — the same
	 * orientation the edges went in, not the reversed one handed to ELK. A caller should not have to
	 * know that this function flips edges in order to find its own edge's route.
	 */
	routes: Map<string, ElkRoute>;
}

/** The route key both sides agree on. Exported so a caller cannot get the spelling wrong. */
export function routeKey(source: string, target: string): string {
	return `${source}>${target}`;
}

/**
 * Place `ids` under `edges`, returning top-left coordinates keyed by id plus the route ELK computed
 * for each edge.
 *
 * Edges are DERIVATION-oriented on the way in — `source` derived from `target`, the same convention
 * `depths()` reads — and are reversed here so ELK lays the graph out in reading order, upstream on
 * the left. Getting that backwards silently mirrors the whole picture, which looks like a layout bug
 * rather than an orientation one.
 */
export async function elkLayout(
	ids: string[],
	edges: readonly LayoutEdge[],
	opts: ElkLayoutOptions = {},
): Promise<ElkLayoutResult> {
	const placed = new Map<string, Placed>();
	const groups = new Map<string, PlacedGroup>();
	const routes = new Map<string, ElkRoute>();
	if (ids.length === 0) return { nodes: placed, groups, routes };

	const {
		direction = 'RIGHT',
		layerGap = 90,
		nodeGap = 34,
		size,
		parentOf,
		groupPadding = 12,
		groupLabelHeight = 26,
	} = opts;
	const known = new Set(ids);

	/**
	 * Build the child list, nesting under containers when `parentOf` asks for it.
	 *
	 * Containers are created in first-seen order and a node with no parent stays at the top level, so
	 * a caller can group SOME nodes without having to invent a container for the rest.
	 */
	const containers = new Map<string, ElkNode>();
	const children: ElkNode[] = [];
	for (const id of ids) {
		const box = size?.(id) ?? { width: DEFAULT_W, height: DEFAULT_H };
		const parent = parentOf?.(id);
		if (parent === undefined) {
			children.push({ id, ...box });
			continue;
		}
		let container = containers.get(parent);
		if (!container) {
			container = {
				id: parent,
				children: [],
				layoutOptions: {
					// The container's OWN inner layout. Without this the children are laid out by the
					// root's algorithm and the container is sized around whatever that produced.
					'elk.algorithm': 'layered',
					'elk.direction': direction,
					'elk.padding': `[top=${groupPadding + groupLabelHeight},left=${groupPadding},bottom=${groupPadding},right=${groupPadding}]`,
				},
			};
			containers.set(parent, container);
			children.push(container);
		}
		container.children?.push({ id, ...box });
	}

	// The edges actually handed to ELK, in ELK order, so each `e<i>` can be mapped back to the
	// caller's own pair after layout.
	const routed = edges.filter(
		(e) => e.source !== e.target && known.has(e.source) && known.has(e.target),
	);

	// TYPED AS `ElkNode`, not inferred from the literal. Inference narrows `edges` to the object
	// shape written here, which has no `sections` — so the routes ELK writes BACK onto the graph are
	// invisible to the type system and unreadable without a cast.
	const graph: ElkNode = {
		id: 'root',
		layoutOptions: {
			'elk.algorithm': 'layered',
			'elk.direction': direction,
			'elk.layered.spacing.nodeNodeBetweenLayers': String(layerGap),
			'elk.spacing.nodeNode': String(nodeGap),
			// THE THREE BELOW RESTATE ELK's OWN DEFAULTS. They are kept because this file's job is to
			// be read against Marquez's `useLayout.ts`, and a reader comparing the two needs to see
			// what rask relies on — but none of them is a tuning decision, and an earlier revision of
			// this comment claimed all three were. Leave-one-out on a lineage-shaped probe: deleting
			// any one changes the output by nothing (1400×416 either way).
			//
			// Measured against elkjs 0.12.0 — re-measure before trusting this after an upgrade, since
			// a default that moves upstream would silently change the picture with no diff here.
			'elk.edgeRouting': 'ORTHOGONAL',
			'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
			// Marquez turns this OFF (`separateConnectedComponents: 'false'` +
			// `hierarchyHandling: 'INCLUDE_CHILDREN'`, which are redundant with each other), and that
			// is the single largest difference between the two pictures: a real estate is mostly
			// UNCONNECTED nodes (measured: 20 of 51 datasets), and separating them is what keeps them
			// packed instead of strung down one column. rask gets that by NOT deviating.
			'elk.separateConnectedComponents': 'true',
			'elk.padding': '[top=20,left=20,bottom=20,right=20]',
			// ONLY when there are containers. `INCLUDE_CHILDREN` is what lets one pass lay out a
			// container and its children together AND lets an edge cross a container boundary — but
			// it also forces component separation off, which for a FLAT graph is a regression, not a
			// no-op: a real estate is mostly unconnected nodes and separating them is what keeps them
			// packed rather than strung down one column.
			...(containers.size > 0 ? { 'elk.hierarchyHandling': 'INCLUDE_CHILDREN' } : {}),
		},
		children,
		// REVERSED: derivation-oriented in, reading-oriented out.
		edges: routed.map((e, i) => ({ id: `e${i}`, sources: [e.target], targets: [e.source] })),
	};

	elk ??= new ELK();
	const laid = await elk.layout(graph);
	// ELK reports TOP-LEFT, which is Svelte Flow's own convention — no centre offset to undo.
	// A container is recorded as a GROUP and recursed into; its children's coordinates are left
	// parent-relative, exactly as they arrive and exactly as Svelte Flow wants them.
	for (const child of laid.children ?? []) {
		if (child.x == null || child.y == null) continue;
		if (containers.has(child.id)) {
			groups.set(child.id, {
				x: child.x,
				y: child.y,
				width: child.width ?? DEFAULT_W,
				height: child.height ?? DEFAULT_H,
			});
			for (const inner of child.children ?? []) {
				if (inner.x != null && inner.y != null) placed.set(inner.id, { x: inner.x, y: inner.y });
			}
			continue;
		}
		placed.set(child.id, { x: child.x, y: child.y });
	}
	// ELK returns edges in the order they were given, but it is not contractually required to, so
	// the index is recovered from the id this function assigned rather than from array position.
	for (const edge of laid.edges ?? []) {
		const index = Number(String(edge.id).slice(1));
		const original = routed[index];
		// ONE section only. `sections` is a list because ELK models hyperedges (several sources or
		// targets); every edge here has exactly one of each, so a multi-section result would mean the
		// graph handed in was not the graph this describes — skip rather than draw a guess.
		const section = edge.sections?.length === 1 ? edge.sections[0] : undefined;
		if (!original || !section?.startPoint || !section.endPoint) continue;
		routes.set(routeKey(original.source, original.target), {
			start: { x: section.startPoint.x, y: section.startPoint.y },
			bendPoints: (section.bendPoints ?? []).map((point) => ({ x: point.x, y: point.y })),
			end: { x: section.endPoint.x, y: section.endPoint.y },
		});
	}
	return { nodes: placed, groups, routes };
}
