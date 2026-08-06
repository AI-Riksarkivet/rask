/**
 * Shared vocabulary for the studio flow builder — node kinds + per-node
 * config/runtime/payload shapes. Kept in its own module so the graph store AND
 * the valibot persistence schema can both import them without an import cycle
 * (the graph re-exports the public ones, so components import from graph.svelte).
 *
 * The builder is an ONLINE sandbox: edges carry a concrete `FlowPayload`
 * (image bytes or text) from node to node, and a Model node POSTs the payload
 * it receives to a live Ray Serve app through the zone's `/api/infer` BFF.
 */

/** The node kinds, as a runtime list (drives the persistence schema + palette). */
export const NODE_KINDS = [
	'image',
	'text',
	'dataset',
	'prompt',
	'model',
	'mcp',
	'alto',
	'extract',
	'regex',
	'compare',
	'inspect',
] as const;
/** A node's `type` (used to pick its component) is its kind. */
export type NodeKind = (typeof NODE_KINDS)[number];

/** Runtime guard for xyflow's `Node.type: string | undefined`. */
export function isNodeKind(v: string | undefined): v is NodeKind {
	return v !== undefined && (NODE_KINDS as readonly string[]).includes(v);
}

export type RunStatus = 'idle' | 'running' | 'done' | 'error';

/** How a Model node builds its request body. */
export const BODY_MODES = ['upstream', 'json'] as const;
export type BodyMode = (typeof BODY_MODES)[number];

/** What a Regex node does with its pattern. */
export const REGEX_MODES = ['extract', 'replace'] as const;
export type RegexMode = (typeof REGEX_MODES)[number];

/** Starting point for a JSON body — deliberately generic. Serve deployments publish no request
 *  schema the frontend could read, so the sandbox hands the user a template to edit rather than
 *  pretending to know what `gemma-31b` wants. */
export const DEFAULT_JSON_BODY = '{\n  "text": "{{input}}"\n}';

/** Starting point for a Prompt node. */
export const DEFAULT_PROMPT = 'Summarise the following transcription:\n\n{{input}}';

/** What travels along an edge: the concrete value a node produced. */
export type FlowPayload =
	| { kind: 'bytes'; bytes: ArrayBuffer; mime: string; label: string }
	| { kind: 'text'; text: string; mime: string; label: string };

/** A node's output — `failed` blocks every dependent instead of running it on
 *  partial input (same rule as the explorer editor this is modeled on). */
export interface NodeOutput {
	payload: FlowPayload | null;
	failed?: boolean;
}

/** Per-node user input. One flat shape covers every kind (each node UI only
 *  edits the fields it cares about) — the explorer editor's POC shortcut,
 *  kept while the kind set is this small; unused fields stay at harmless
 *  defaults. */
export interface NodeConfig {
	/** ergonomics: optional custom title; disabled nodes are bypassed on run. */
	label: string;
	enabled: boolean;
	/** image: the uploaded file. Never persisted (a File can't serialise) —
	 *  `fileName` survives a reload so the node can prompt for re-upload. */
	file: File | null;
	fileName: string;
	/** text: the inline text fed downstream. */
	text: string;
	/** model: the Serve app to POST to — its route prefix without the leading
	 *  slash (e.g. 'htrflow'), picked from live discovery or typed by hand. */
	app: string;
	/** model: the `?name=` passthrough (names the page in ALTO output). */
	requestName: string;
	/** model: the path WITHIN the Serve app — `/v1/chat/completions`, `/rerank`, or empty for the
	 *  app root. Load-bearing: a Serve app's route_prefix only reaches its root, and every
	 *  OpenAI-compatible deployment (vLLM: gemma, qwen-embed) puts its real endpoints under `/v1/...`,
	 *  so without this the whole class of app is uncallable. Measured against the live cluster
	 *  2026-08-06: POST /gemma-31b 404s, POST /gemma-31b/v1/chat/completions answers. */
	modelPath: string;
	/** model: what to put in the request BODY. `upstream` forwards the incoming payload verbatim —
	 *  right for a bytes-in app like `/htrflow`, which reads the raw image. `json` sends
	 *  `bodyTemplate` instead, which is what an LLM / embedding / rerank deployment expects; those
	 *  are the majority of a real cluster's apps and were uncallable while raw-forward was the only
	 *  mode. The template is authored by the user rather than guessed from the app name, because
	 *  Serve deployments declare no request schema anywhere the frontend can read. */
	bodyMode: BodyMode;
	/** model: the JSON body sent when `bodyMode === 'json'`. `{{input}}` interpolates the upstream
	 *  text payload, JSON-escaped. */
	bodyTemplate: string;
	/** prompt: a text template composed from the upstream payload — `{{input}}` is substituted
	 *  verbatim (no JSON escaping; this produces prose, not a wire body). */
	promptTemplate: string;
	/** dataset: the governed table to read rows from, as `warehouse.namespace.table`. SCAFFOLD —
	 *  the node emits a description of the selection, not rows. Reading real rows needs the
	 *  explorer's Arrow transport (`/api/explorer/**`), which is a keep-bytes lane and therefore a
	 *  `+server.ts` of its own; the shape is here so the lakehouse→flow seam is visible. */
	datasetTable: string;
	/** mcp: the MCP server to call and the tool on it, plus a JSON argument object. SCAFFOLD — see
	 *  the `mcp` case in `executor.ts` for why it refuses rather than calls: MCP servers in this repo
	 *  are Claude Code's session tooling (`make claude-bootstrap`), not a service the app can reach,
	 *  so invoking one needs an MCP CLIENT and a stdio/SSE transport server-side that does not exist
	 *  yet. The shape is here so an agent-shaped flow is expressible on the canvas. */
	mcpServer: string;
	mcpTool: string;
	mcpArgs: string;
	/** extract: a dotted path into the upstream JSON — `choices[0].message.content`. This is the node
	 *  that makes an LLM answer CHAINABLE: a Serve app returns JSON, and without pulling the field
	 *  out, everything downstream sees an envelope instead of the text. */
	extractPath: string;
	/** regex: the pattern, and what to do with it. `extract` emits the matches (capture group 1 when
	 *  present, else the whole match); `replace` emits the input with matches replaced. */
	regexPattern: string;
	regexReplace: string;
	regexMode: RegexMode;
}

/** Per-node RUN state, keyed by node id. */
export interface NodeRuntime {
	status: RunStatus;
	error: string | null;
	/** Last run wall-clock in ms (Model node). */
	ms: number | null;
	/** Last output this node produced — reused as the upstream input when a
	 *  downstream node is run individually. */
	output: NodeOutput | null;
	/** Fingerprint of the config + incoming edges the output was computed from;
	 *  a mismatch at reuse time means the node was edited or rewired since. */
	outputKey: string | null;
	/** True when an upstream node re-ran after this node's last run. */
	stale: boolean;
}

/** Short human summary of a payload (chips, Inspect header). */
export function payloadSummary(p: FlowPayload | null): string {
	if (!p) return 'no output';
	if (p.kind === 'bytes')
		return `${p.mime || 'bytes'} · ${(p.bytes.byteLength / 1024).toFixed(1)} kB`;
	return `${p.mime || 'text'} · ${p.text.length} chars`;
}
