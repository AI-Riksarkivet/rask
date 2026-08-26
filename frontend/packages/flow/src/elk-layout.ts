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
 * its routing as splines — that was simply wrong. rask asks ELK for the same orthogonal route and
 * then discards the geometry (only `x`/`y` are read below), letting `smoothstep` redraw its own right
 * angles; the two usually coincide, but the bend points ELK computed are thrown away.
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
