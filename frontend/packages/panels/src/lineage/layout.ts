/**
 * Layered layout for the lineage DAG — the ordering step the graph was missing.
 *
 * **Why not a force layout.** Lineage is a DAG whose edges carry direction: `derived_from` means
 * bronze precedes silver precedes gold. A force simulation treats edges as springs and has no notion
 * of direction, so it settles into a blob and destroys the left-to-right derivation reading — the one
 * thing a lineage graph is for. It is also non-deterministic (a different arrangement every load, so
 * no spatial memory) and animated (it visibly settles). d3-force is the right tool for undirected,
 * clustered graphs; this is not one.
 *
 * **Why not dagre or ELK.** They are the correct family — Sugiyama layered layout — and would be
 * better than this at scale. They do not fit: `@dagrejs/dagre` unpacks to 1.1 MB and elkjs to 7.9 MB
 * against ~7 KB of deferred-bundle headroom in this zone (`budget.json`), for a graph the store caps
 * at 60 nodes (`MAX_GRAPH`). The part actually missing was one phase of the algorithm, not the whole
 * library. If the cap is ever lifted past a few hundred nodes, dagre behind a dynamic import is the
 * upgrade — it also does proper coordinate assignment and routes long edges through dummy nodes,
 * which this deliberately does not.
 *
 * **What was wrong before.** The old placer did layer ASSIGNMENT correctly (longest-path depth) and
 * then placed nodes within a layer in *iteration order*. Nothing tried to put connected nodes near
 * each other, so edges crossed arbitrarily — that is the whole of "the graph looks weird". It also
 * packed every depth-0 node into a `ceil(sqrt(n))` grid spanning several columns, which fixed a real
 * problem (60 roots stacked in one column is a 6,600 px tower) by creating another: roots scattered
 * over four columns fan their edges diagonally across the canvas.
 *
 * **What this does instead**, in the classic three phases:
 *
 *  1. **Partition.** Nodes with no edges at all are not part of the DAG and are the majority of a real
 *     estate. They are parked in a compact grid below the graph instead of padding its first column.
 *  2. **Layer** by longest-path depth (unchanged — it was correct).
 *  3. **Order** within each layer by the barycentre of each node's neighbours in the previous layer,
 *     swept forward then backward a few times. This is Sugiyama's crossing-minimisation phase and the
 *     step that was missing.
 */
/**
 * The minimum an edge must carry to be laid out. Declared structurally rather than importing
 * `GraphEdge`: this module only ever reads the two endpoints, and the JOBS plane synthesises its own
 * edges (job → producing-job) which have no `kind`. Demanding the full type would force a meaningless
 * field on a caller that has nothing to put in it.
 */
export interface LayoutEdge {
	source: string;
	target: string;
}

export interface Placed {
	x: number;
	y: number;
}

/** Card pitch — the medallion/job cards are 200px wide and ~64px tall. */
const COL_W = 240;
const ROW_H = 110;
const X0 = 30;
const Y0 = 40;
/** Sweeps of the barycentre heuristic. Four is the usual point of diminishing returns. */
const SWEEPS = 4;

/**
 * Longest-path depth per node from the DERIVED_FROM edges (`source` derived from `target`, so
 * `target` is upstream and sits one column left). Iteration-capped: a cyclic payload cannot loop.
 */
export function depths(ids: string[], edges: readonly LayoutEdge[]): Map<string, number> {
	const depth = new Map<string, number>(ids.map((id) => [id, 0]));
	for (let i = 0; i < depth.size; i += 1) {
		let changed = false;
		for (const e of edges) {
			const next = (depth.get(e.target) ?? 0) + 1;
			if (e.source !== e.target && next > (depth.get(e.source) ?? 0)) {
				depth.set(e.source, next);
				changed = true;
			}
		}
		if (!changed) break;
	}
	return depth;
}

/**
 * Positions for every node, keyed by id.
 *
 * Deterministic: the same graph always lays out identically, so a user's spatial memory survives a
 * poll. That is the property a force simulation cannot offer.
 */
export function layout(
	ids: string[],
	edges: readonly LayoutEdge[],
	depthOf: (id: string) => number,
): Map<string, Placed> {
	const placed = new Map<string, Placed>();
	const degree = new Map<string, number>(ids.map((id) => [id, 0]));
	for (const e of edges) {
		if (e.source === e.target) continue;
		degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
		degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
	}

	// (1) PARTITION. An isolated table is not part of the derivation story and must not stretch the
	// graph's first column; the old sqrt-grid mixed the two and scattered the real roots.
	const connected = ids.filter((id) => (degree.get(id) ?? 0) > 0);
	const isolated = ids.filter((id) => (degree.get(id) ?? 0) === 0);

	// (2) LAYER.
	const layers: string[][] = [];
	for (const id of connected) {
		const d = depthOf(id);
		(layers[d] ??= []).push(id);
	}

	// Neighbours in the PREVIOUS layer, which is what a node's barycentre is measured against.
	const upstream = new Map<string, string[]>();
	const downstream = new Map<string, string[]>();
	for (const e of edges) {
		if (e.source === e.target) continue;
		(upstream.get(e.source) ?? upstream.set(e.source, []).get(e.source)!).push(e.target);
		(downstream.get(e.target) ?? downstream.set(e.target, []).get(e.target)!).push(e.source);
	}

	const rank = new Map<string, number>();
	for (const layer of layers) layer?.forEach((id, i) => rank.set(id, i));

	/** Mean rank of a node's neighbours in an adjacent layer; `null` when it has none there. */
	const barycentre = (id: string, side: Map<string, string[]>): number | null => {
		const ns = (side.get(id) ?? [])
			.map((n) => rank.get(n))
			.filter((r): r is number => r !== undefined);
		return ns.length === 0 ? null : ns.reduce((a, b) => a + b, 0) / ns.length;
	};

	// (3) ORDER. Sweep forward (order each layer by its upstream neighbours) then backward, repeatedly.
	// A node with no neighbour on the sweep's side keeps its current rank, so the sort stays stable.
	for (let s = 0; s < SWEEPS; s += 1) {
		const forward = s % 2 === 0;
		const indices = forward ? layers.keys() : [...layers.keys()].reverse();
		for (const d of indices) {
			const layer = layers[d];
			if (layer === undefined || layer.length < 2) continue;
			const side = forward ? upstream : downstream;
			const keyed = layer.map((id) => ({ id, b: barycentre(id, side) ?? rank.get(id) ?? 0 }));
			keyed.sort((a, b) => a.b - b.b);
			layers[d] = keyed.map((k) => k.id);
			layers[d]?.forEach((id, i) => rank.set(id, i));
		}
	}

	// Coordinates. Each layer is centred on the tallest one, so a wide fan-in does not sit against the
	// top edge while its single consumer floats in the middle of the next column.
	const tallest = layers.reduce((m, l) => Math.max(m, l?.length ?? 0), 0);
	layers.forEach((layer, d) => {
		if (layer === undefined) return;
		const offset = ((tallest - layer.length) / 2) * ROW_H;
		layer.forEach((id, i) => placed.set(id, { x: X0 + d * COL_W, y: Y0 + offset + i * ROW_H }));
	});

	// Isolated nodes: a compact grid parked BELOW the graph, roughly as wide as the graph is.
	const graphCols = Math.max(1, layers.length);
	const graphBottom = Y0 + tallest * ROW_H + ROW_H;
	isolated.forEach((id, i) => {
		placed.set(id, {
			x: X0 + (i % graphCols) * COL_W,
			y: graphBottom + Math.floor(i / graphCols) * ROW_H,
		});
	});

	return placed;
}
