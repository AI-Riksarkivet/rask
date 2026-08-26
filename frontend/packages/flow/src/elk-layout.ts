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
}

/**
 * Place `ids` under `edges`, returning top-left coordinates keyed by id.
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
): Promise<Map<string, Placed>> {
	const placed = new Map<string, Placed>();
	if (ids.length === 0) return placed;

	const { direction = 'RIGHT', layerGap = 90, nodeGap = 34, size } = opts;
	const known = new Set(ids);

	const graph = {
		id: 'root',
		layoutOptions: {
			'elk.algorithm': 'layered',
			'elk.direction': direction,
			'elk.layered.spacing.nodeNodeBetweenLayers': String(layerGap),
			'elk.spacing.nodeNode': String(nodeGap),
			// ORTHOGONAL over Marquez's SLOPPY splines: rask's edges are already `smoothstep`, and
			// right angles read as a pipeline rather than as a bundle of wires.
			'elk.edgeRouting': 'ORTHOGONAL',
			// Brandes-Köpf placement is what actually pulls a node level with the one it feeds — the
			// single phase the hand-rolled placer skipped.
			'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
			// A real estate is mostly UNCONNECTED nodes (measured: 20 of 51 datasets), and packing
			// them is a first-class ELK job. The placer this replaces bolted them into a grid as wide
			// as the graph was deep, so an unrelated block grew with the graph.
			'elk.separateConnectedComponents': 'true',
			'elk.padding': '[top=20,left=20,bottom=20,right=20]',
		},
		children: ids.map((id) => ({ id, ...(size?.(id) ?? { width: DEFAULT_W, height: DEFAULT_H }) })),
		edges: edges
			.filter((e) => e.source !== e.target && known.has(e.source) && known.has(e.target))
			// REVERSED: derivation-oriented in, reading-oriented out.
			.map((e, i) => ({ id: `e${i}`, sources: [e.target], targets: [e.source] })),
	};

	elk ??= new ELK();
	const laid = await elk.layout(graph);
	// ELK reports TOP-LEFT, which is Svelte Flow's own convention — no centre offset to undo.
	for (const child of laid.children ?? []) {
		if (child.x != null && child.y != null) placed.set(child.id, { x: child.x, y: child.y });
	}
	return placed;
}
