/**
 * "Run this flow from outside the browser" — the snippet that turns a drawn graph into a call.
 *
 * The flows service already accepts the whole graph (`POST /api/flows/runs`, see services/flows),
 * so a built flow is callable today; nothing surfaced that. This is the Langflow affordance of
 * publishing a flow, in its honest minimal form: generate the request, let the user copy it.
 *
 * Pure functions over the graph's own wire shape, so they are testable without the store and cannot
 * print a body the service would reject — the shape here is exactly `flows.models.RunRequest`.
 */

/** The service's `RunRequest`: the graph, plus per-source-node text seeds. */
export interface RunRequestBody {
	graph: {
		nodes: { id: string; kind: string; config: Record<string, unknown> }[];
		edges: { source: string; target: string }[];
	};
	seeds: Record<string, string>;
}

/** Build the request body from the editor's own node/edge/config state.
 *
 *  `image` nodes are dropped with their edges: their payload is browser-only (a File), and the
 *  service fails an `image` node BY DESIGN rather than pretending to have bytes it never got. A
 *  snippet that included them would produce a run that always fails, so the export is honest about
 *  being the server-runnable SUBSET of the flow. */
export function buildRunRequest(
	nodes: { id: string; type?: string | undefined }[],
	edges: { source: string; target: string }[],
	// `unknown` values, not an index-signature type: the editor's own `NodeConfig` is an interface
	// and would not satisfy `Record<string, Record<string, unknown>>`. Each value is narrowed below.
	config: Record<string, unknown>,
): { body: RunRequestBody; droppedImageNodes: string[] } {
	const dropped = nodes.filter((n) => n.type === 'image').map((n) => n.id);
	const droppedSet = new Set(dropped);
	const kept = nodes.filter((n) => !droppedSet.has(n.id) && typeof n.type === 'string');
	return {
		body: {
			graph: {
				nodes: kept.map((n) => ({
					id: n.id,
					kind: n.type as string,
					config: serialisableConfig(config[n.id]),
				})),
				edges: edges
					.filter((e) => !droppedSet.has(e.source) && !droppedSet.has(e.target))
					.map((e) => ({ source: e.source, target: e.target })),
			},
			seeds: {},
		},
		droppedImageNodes: dropped,
	};
}

/** One node's config, minus anything that cannot cross the wire. `file` is a `File` — browser-only,
 *  and `JSON.stringify` would silently flatten it to `{}`, so it is dropped explicitly rather than
 *  shipped as a misleading empty object. */
function serialisableConfig(raw: unknown): Record<string, unknown> {
	if (typeof raw !== 'object' || raw === null) return {};
	const { file: _file, ...rest } = raw as Record<string, unknown>;
	return rest;
}

/** A copy-pasteable curl for the run. `base` defaults to the gateway's public path. */
export function curlSnippet(body: RunRequestBody, base = 'http://localhost:8888'): string {
	return [
		`curl -sS -X POST ${base}/api/flows/runs \\`,
		`  -H 'content-type: application/json' \\`,
		`  -d '${JSON.stringify(body)}'`,
	].join('\n');
}

/** The same call in Python, using httpx (the estate's HTTP client). */
export function pythonSnippet(body: RunRequestBody, base = 'http://localhost:8888'): string {
	return [
		'import httpx',
		'',
		`payload = ${JSON.stringify(body, null, 4).replace(/"/g, '"')}`,
		'',
		`resp = httpx.post("${base}/api/flows/runs", json=payload, timeout=300)`,
		'resp.raise_for_status()',
		'print(resp.json())',
	].join('\n');
}
