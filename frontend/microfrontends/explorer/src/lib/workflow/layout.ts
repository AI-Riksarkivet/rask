/**
 * Auto-layout the node graph with elkjs — drives the toolbar's "Tidy" button.
 * elkjs (over dagre) gives us ORTHOGONAL edge routing and correct sub-flow
 * layouting, which dagre can't do; the cost is that layout is async.
 * Pure: takes nodes/edges, returns nodes with new positions.
 */
import ELK from 'elkjs/lib/elk.bundled.js';
import type { Edge, Node } from '@xyflow/svelte';

/** One ELK instance (the bundled build runs layout in-process, no web worker),
 *  created LAZILY on first layout: the constructor probes `Worker`, which does not
 *  exist server-side — a module-scope `new ELK()` would break SSR of any page that
 *  imports this module (autoLayout itself only ever runs from the Tidy button). */
let elk: InstanceType<typeof ELK> | null = null;

/** Fallback box when a node hasn't been measured yet (pre-first-render). */
const DEFAULT_W = 256;
const DEFAULT_H = 150;

/** Re-position `nodes` with elkjs' layered algorithm. `direction` maps to ELK's
 *  ('LR' = left→right = ELK `RIGHT`, the natural reading order for pipelines). */
export async function autoLayout(
	nodes: Node[],
	edges: Edge[],
	direction: 'LR' | 'TB' = 'LR',
): Promise<Node[]> {
	if (!nodes.length) return nodes;

	const size = (n: Node): { width: number; height: number } => ({
		width: n.measured?.width ?? n.width ?? DEFAULT_W,
		height: n.measured?.height ?? n.height ?? DEFAULT_H,
	});

	const nodeIds = new Set(nodes.map((n) => n.id));
	const graph = {
		id: 'root',
		layoutOptions: {
			'elk.algorithm': 'layered',
			'elk.direction': direction === 'LR' ? 'RIGHT' : 'DOWN',
			'elk.layered.spacing.nodeNodeBetweenLayers': '90', // gap between ranks
			'elk.spacing.nodeNode': '40', // gap between siblings
			'elk.edgeRouting': 'ORTHOGONAL', // clean right-angle edges (dagre couldn't)
			'elk.padding': '[top=20,left=20,bottom=20,right=20]',
		},
		children: nodes.map((n) => ({ id: n.id, ...size(n) })),
		edges: edges
			.filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
			.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
	};

	elk ??= new ELK();
	const laid = await elk.layout(graph);
	// elkjs reports TOP-LEFT positions — Svelte Flow's convention — so no
	// center→top-left offset (dagre needed one; elkjs does not).
	const positioned = new Map((laid.children ?? []).map((c) => [c.id, c]));
	return nodes.map((n) => {
		const p = positioned.get(n.id);
		return p && p.x != null && p.y != null ? { ...n, position: { x: p.x, y: p.y } } : n;
	});
}
