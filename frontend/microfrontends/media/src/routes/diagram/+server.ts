// Server-side-rendered static pipeline diagram — the Svelte Flow SSR contract:
// render the graph to an HTML string on the server (no browser), for docs / OG
// images / non-JS. Lives OUTSIDE /api (which is proxied to the FastAPI backend).
// Node dimensions + handle positions are EXPLICIT (there is no layout engine
// server-side); we lay the medallion lane out left→right by hand.
import { render } from 'svelte/server';
import { Position, type Edge, type Node } from '@xyflow/svelte';
// Inline the Svelte Flow stylesheet into the SSR head — component CSS imports are
// injected client-side by Vite, so a pure server render would otherwise emit
// unstyled (invisible) nodes.
import xyflowCss from '@xyflow/svelte/dist/style.css?inline';
import Flow from '$lib/diagram/Flow.svelte';
import type { RequestHandler } from './$types';

const W = 150;
const H = 56;

/** The medallion lane as a static diagram (the default when no graph is posted). */
function medallionLane(): { nodes: Node[]; edges: Edge[] } {
	const stages = [
		'bronze\n(raw media)',
		'silver\n(derivers)',
		'gold\n(QC-curated)',
		'app\n(viewer/search)',
	];
	const nodes: Node[] = stages.map((label, i) => ({
		id: String(i),
		type: 'default',
		position: { x: i * (W + 80), y: 0 },
		data: { label },
		width: W,
		height: H,
		// Explicit handles so edges route without client-side measurement (SSR).
		handles: [
			{ type: 'target', position: Position.Left, x: 0, y: H / 2 },
			{ type: 'source', position: Position.Right, x: W, y: H / 2 },
		],
	}));
	const edges: Edge[] = stages.slice(1).map((_v, i) => ({
		id: `e${i}`,
		source: String(i),
		target: String(i + 1),
	}));
	return { nodes, edges };
}

export const GET: RequestHandler = () => {
	const { nodes, edges } = medallionLane();
	const width = 4 * (W + 80);
	const height = 200;
	const { body, head } = render(Flow, { props: { nodes, edges, width, height } });
	const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>lance-media pipeline</title>${head}<style>${xyflowCss}</style><style>html,body{margin:0}.diagram{width:${width}px;height:${height}px}</style></head><body><div class="diagram">${body}</div></body></html>`;
	return new Response(html, { headers: { 'content-type': 'text/html; charset=utf-8' } });
};

/** No prerender + this is a dynamic server route (SSR of the graph on demand). */
export const prerender = false;
