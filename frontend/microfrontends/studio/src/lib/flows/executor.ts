/**
 * The flow execution engine — a parallel dataflow over the node graph, kept
 * separate from the store so the algorithm is decoupled from (and testable
 * without) FlowsGraph's internals. It reads topology + config and writes
 * results back through the narrow `RunDeps` seam; it never touches the class.
 *
 * Same engine shape as the explorer workflow editor (runGraph / runSubgraph /
 * topoOrder / fingerprint cache), with the domain switch swapped for studio's
 * online-sandbox kinds — and unlike the original, this copy has unit tests
 * (executor.test.ts) because the seam exists exactly for that.
 */
import type { Edge, Node } from '@xyflow/svelte';
import type { FlowPayload, NodeConfig, NodeKind, NodeOutput, NodeRuntime } from './types';

/** A Model node's invocation, routed through the zone's `/api/infer` BFF —
 *  injected via RunDeps so tests run the engine with no network at all. */
export interface InvokeRequest {
	app: string;
	name: string;
	payload: FlowPayload;
}

/** Everything the executor needs from the graph — a narrow interface so the
 *  store stays in control of state while the algorithm lives here. */
export interface RunDeps {
	nodes: Node[];
	edges: Edge[];
	config(id: string): NodeConfig;
	kindOf(id: string): NodeKind | null;
	patchRuntime(id: string, patch: Partial<NodeRuntime>): void;
	/** A node's reusable last output for a partial run, or null when it must be
	 *  recomputed (never ran, failed, stale, or edited/rewired since — compared
	 *  via `fingerprint`). */
	cachedOutput(id: string): NodeOutput | null;
	/** Current fingerprint of a node's output-affecting config + incoming edges. */
	fingerprint(id: string): string;
	/** POST a payload to a live Serve app; resolves to the response payload. */
	invoke(req: InvokeRequest): Promise<FlowPayload>;
	/** Append a line to the run log — the transcript the bottom drawer shows. Part of the SEAM, not
	 *  a global import, so the engine stays testable with a stub and cannot reach a store. */
	log(level: 'info' | 'warn' | 'error', message: string, nodeId?: string): void;
}

export const CYCLE_ERROR = 'The graph has a cycle — remove a connection and run again.';

/**
 * Execute the whole graph as a dataflow: every node runs as soon as its OWN
 * predecessors resolve, so independent branches run concurrently. Promises are
 * seeded in topological order, so when a node reads its predecessors' promises
 * they already exist. A node failure never rejects (it records an error on that
 * node) but it BLOCKS everything downstream. Returns a cycle-error message to
 * surface, or null on success.
 */
export async function runGraph(deps: RunDeps): Promise<string | null> {
	const ids = deps.nodes.map((n) => n.id);
	const incoming = incomingMap(deps.edges, new Set(ids));

	const order = topoOrder(ids, incoming);
	if (!order) return CYCLE_ERROR;

	const outputs = new Map<string, Promise<NodeOutput>>();
	for (const id of order) {
		// `order` is topological, so every predecessor's promise is already set.
		outputs.set(id, computeNode(deps, id, incoming.get(id) ?? [], outputs));
	}
	await Promise.all(outputs.values());
	return null;
}

/**
 * Run ONE node (the per-node ▶ button): its upstream closure executes too, but
 * a predecessor with a reusable cached output is read instead of re-run. The
 * target itself ALWAYS recomputes; `fresh` forces the whole upstream branch to
 * as well. Returns the ids actually executed.
 */
export async function runSubgraph(
	deps: RunDeps,
	targetId: string,
	opts: { fresh?: boolean } = {},
): Promise<{ error: string | null; ran: string[] }> {
	const ids = new Set(deps.nodes.map((n) => n.id));
	if (!ids.has(targetId)) return { error: null, ran: [] };
	const incoming = incomingMap(deps.edges, ids);

	// Upstream closure: the target plus everything it (transitively) reads.
	const closure = new Set<string>([targetId]);
	const stack = [targetId];
	while (stack.length) {
		const id = stack.pop()!;
		for (const p of incoming.get(id) ?? []) {
			if (!closure.has(p)) {
				closure.add(p);
				stack.push(p);
			}
		}
	}

	const order = topoOrder([...closure], incoming);
	if (!order) return { error: CYCLE_ERROR, ran: [] };

	const outputs = new Map<string, Promise<NodeOutput>>();
	const ran: string[] = [];
	const ranSet = new Set<string>();
	for (const id of order) {
		const preds = incoming.get(id) ?? [];
		// A node whose input is being recomputed THIS run can't serve its cache —
		// it was computed from the predecessor's previous output.
		const predRecomputed = preds.some((p) => ranSet.has(p));
		const cached = id === targetId || opts.fresh || predRecomputed ? null : deps.cachedOutput(id);
		if (cached) {
			outputs.set(id, Promise.resolve(cached));
			continue;
		}
		ran.push(id);
		ranSet.add(id);
		outputs.set(id, computeNode(deps, id, preds, outputs));
	}
	await Promise.all(outputs.values());
	return { error: null, ran };
}

/** target → [sources], restricted to `ids` (drops edges touching unknown nodes). */
function incomingMap(edges: Edge[], ids: Set<string>): Map<string, string[]> {
	const incoming = new Map<string, string[]>([...ids].map((id) => [id, []]));
	for (const e of edges) {
		if (incoming.has(e.target) && ids.has(e.source)) incoming.get(e.target)!.push(e.source);
	}
	return incoming;
}

/** Execute a node once its predecessors' promises resolve, caching its output
 *  on the runtime (and clearing any stale flag) for later partial runs. */
function computeNode(
	deps: RunDeps,
	id: string,
	preds: string[],
	outputs: Map<string, Promise<NodeOutput>>,
): Promise<NodeOutput> {
	return Promise.all(preds.map((p) => outputs.get(p)!)).then(async (predOutputs) => {
		// Fingerprint BEFORE executing: an edit landing mid-run must not stamp an
		// output computed from the old config as fresh under the new key.
		const key = deps.fingerprint(id);
		const out = await runNode(deps, id, predOutputs);
		deps.patchRuntime(id, { output: out, outputKey: key, stale: false });
		return out;
	});
}

/** Topologically order `ids` by the `incoming` edges among them; returns null
 *  if they contain a cycle. */
function topoOrder(ids: string[], incoming: Map<string, string[]>): string[] | null {
	const members = new Set(ids);
	const deg = new Map<string, number>(
		ids.map((id) => [id, (incoming.get(id) ?? []).filter((p) => members.has(p)).length]),
	);
	const outgoing = new Map<string, string[]>();
	for (const id of ids) {
		for (const p of incoming.get(id) ?? []) {
			if (!members.has(p)) continue;
			const list = outgoing.get(p);
			if (list) list.push(id);
			else outgoing.set(p, [id]);
		}
	}
	const queue = ids.filter((id) => (deg.get(id) ?? 0) === 0);
	const order: string[] = [];
	while (queue.length) {
		const id = queue.shift()!;
		order.push(id);
		for (const t of outgoing.get(id) ?? []) {
			const d = (deg.get(t) ?? 0) - 1;
			deg.set(t, d);
			if (d === 0) queue.push(t);
		}
	}
	return order.length === ids.length ? order : null;
}

/** Render a JSON body template, substituting `{{input}}` with the upstream TEXT payload — JSON-escaped
 *  via `JSON.stringify` minus its wrapping quotes, so a newline or a quote in the input cannot break
 *  out of the string it lands in. A bytes payload has no text form, so it interpolates as empty: a
 *  template that does not reference `{{input}}` still works (an embedding call with a fixed probe),
 *  and one that does gets an empty string rather than a mangled body. */
export function renderBody(template: string, input: FlowPayload | null): string {
	const text = input?.kind === 'text' ? input.text : '';
	const escaped = JSON.stringify(text).slice(1, -1);
	return template.replaceAll('{{input}}', escaped);
}

/** Walk a dotted path into a parsed JSON value — `choices[0].message.content`, `data.0.embedding`.
 *  Returns `undefined` for any step that does not exist, so a wrong path is a reportable miss rather
 *  than a crash. Bracket indices and dotted indices both work, because both spellings appear in the
 *  wild and guessing wrong is the most likely thing a user does here. */
export function pickPath(value: unknown, path: string): unknown {
	const steps = path
		.replace(/\[(\d+)\]/g, '.$1')
		.split('.')
		.filter((s) => s !== '');
	let cur: unknown = value;
	for (const step of steps) {
		if (cur === null || cur === undefined) return undefined;
		if (Array.isArray(cur)) {
			const i = Number(step);
			if (!Number.isInteger(i)) return undefined;
			cur = cur[i];
		} else if (typeof cur === 'object') {
			cur = (cur as Record<string, unknown>)[step];
		} else {
			return undefined; // a scalar with path left to walk
		}
	}
	return cur;
}

/** Render an extracted value as the text that travels on: strings pass through unquoted (the whole
 *  point — an LLM's answer should not arrive wrapped in quotes), everything else is JSON. */
export function renderExtracted(value: unknown): string {
	if (typeof value === 'string') return value;
	return JSON.stringify(value, null, 2) ?? '';
}

/** Apply a regex to text. `extract` joins the matches with newlines, preferring capture group 1 when
 *  the pattern has one; `replace` substitutes. Always global, because a node that silently handled
 *  only the first of many matches would be the more surprising behaviour. */
export function applyRegex(
	text: string,
	pattern: string,
	mode: 'extract' | 'replace',
	replacement: string,
): string {
	// Constructed per call, and `g` is forced: a shared RegExp with /g carries lastIndex between
	// calls, which is the classic way this kind of node starts skipping matches.
	const re = new RegExp(pattern, 'g');
	if (mode === 'replace') return text.replace(re, replacement);
	return [...text.matchAll(re)].map((m) => m[1] ?? m[0]).join('\n');
}

/** Side-by-side report for a Compare node — the A/B affordance for "is gemma better than qwen here".
 *  Deliberately a TEXT report rather than a diff widget: it travels down the graph like any other
 *  payload, so it can be inspected, copied or fed onward. */
export function compareTexts(texts: string[]): string {
	const lines: string[] = [`${texts.length} inputs compared`, ''];
	texts.forEach((t, i) => {
		lines.push(`── ${String.fromCharCode(65 + i)} (${t.length} chars) ──`, t, '');
	});
	if (texts.length >= 2) {
		const [a, b] = [texts[0] ?? '', texts[1] ?? ''];
		lines.push(a === b ? '── verdict: A and B are identical ──' : '── verdict: A and B differ ──');
		if (a !== b) {
			const al = a.split('\n');
			const bl = b.split('\n');
			const at = al.findIndex((l, i) => l !== bl[i]);
			if (at >= 0)
				lines.push(
					`first difference at line ${at + 1}:`,
					`A: ${al[at] ?? ''}`,
					`B: ${bl[at] ?? ''}`,
				);
		}
	}
	return lines.join('\n');
}

/** Extract the transcribed text out of an ALTO XML document: one line per
 *  `<TextLine>`, its words joined from the `<String CONTENT="…">` attributes.
 *  Regex-based on purpose — dependency-free and runnable in vitest's node env
 *  (DOMParser doesn't exist there); ALTO's shape is regular enough for a
 *  sandbox transform, and a malformed document just yields fewer lines. */
export function altoToText(xml: string): string {
	const lines: string[] = [];
	const lineRe = /<TextLine\b[\s\S]*?<\/TextLine>/g;
	const wordRe = /<String\b[^>]*\bCONTENT="([^"]*)"/g;
	for (const line of xml.match(lineRe) ?? []) {
		const words: string[] = [];
		for (const m of line.matchAll(wordRe)) words.push(m[1] ?? '');
		if (words.length) lines.push(words.join(' '));
	}
	return lines.join('\n');
}

/** The first concrete payload among the predecessors' outputs — v0 nodes take
 *  a single input (connection validation enforces one incoming edge on
 *  model/alto/inspect, so "first" is not a race). */
function upstreamPayload(predOutputs: NodeOutput[]): FlowPayload | null {
	for (const o of predOutputs) if (o.payload) return o.payload;
	return null;
}

/** Run one node by kind. Isolated — any throw is recorded on the node and
 *  surfaces as a failed empty output, so it never rejects a dependent's
 *  `Promise.all`; the `failed` flag blocks dependents. */
async function runNode(deps: RunDeps, id: string, predOutputs: NodeOutput[]): Promise<NodeOutput> {
	const kind = deps.kindOf(id);
	// kindOf is null only for an unknown/corrupt node id — nothing to run.
	if (kind === null) return { payload: null };
	const cfg = deps.config(id);

	// An upstream failure blocks this node (and, via the flag, everything after
	// it) — even through a disabled node, which only bypasses its OWN work.
	if (predOutputs.some((o) => o.failed)) {
		deps.patchRuntime(id, { status: 'error', error: 'Skipped — an upstream node failed.' });
		deps.log('warn', 'skipped — an upstream node failed', id);
		return { payload: null, failed: true };
	}

	const input = upstreamPayload(predOutputs);

	// Disabled node: bypass it — forward the input, contribute nothing.
	if (!cfg.enabled) {
		deps.patchRuntime(id, { status: 'idle' });
		deps.log('info', 'disabled — forwarding its input unchanged', id);
		return { payload: input };
	}

	try {
		switch (kind) {
			case 'image': {
				if (!cfg.file) {
					// Politely idle, not an error — a half-configured source shouldn't
					// paint the graph red; downstream nodes just see no payload.
					deps.patchRuntime(id, { status: 'idle' });
					return { payload: null };
				}
				const bytes = await cfg.file.arrayBuffer();
				deps.patchRuntime(id, { status: 'done' });
				return {
					payload: {
						kind: 'bytes',
						bytes,
						mime: cfg.file.type || 'application/octet-stream',
						label: cfg.file.name,
					},
				};
			}
			case 'text': {
				const text = cfg.text;
				deps.patchRuntime(id, { status: text.trim() ? 'done' : 'idle' });
				return {
					payload: text.trim() ? { kind: 'text', text, mime: 'text/plain', label: 'text' } : null,
				};
			}
			case 'model': {
				// JSON mode may need no input at all (a fixed probe against an embedding app), so only
				// `upstream` requires one to have arrived.
				if (!input && cfg.bodyMode === 'upstream') {
					deps.patchRuntime(id, { status: 'idle', error: null });
					return { payload: null };
				}
				const app = cfg.app.trim();
				if (!app) throw new Error('Pick a Serve app to call.');
				// What actually goes on the wire: the upstream payload verbatim (a bytes-in app like
				// /htrflow), or the rendered JSON template — which is what makes an LLM / embedding /
				// rerank deployment callable at all.
				const wire: FlowPayload =
					cfg.bodyMode === 'json'
						? {
								kind: 'text',
								text: renderBody(cfg.bodyTemplate, input),
								mime: 'application/json',
								label: 'json',
							}
						: input!;
				deps.patchRuntime(id, { status: 'running', error: null });
				deps.log(
					'info',
					`POST /${app} — ${cfg.bodyMode === 'json' ? 'json body' : wire.kind} (${
						wire.kind === 'bytes' ? `${wire.bytes.byteLength} B` : `${wire.text.length} chars`
					})`,
					id,
				);
				const t0 = performance.now();
				const payload = await deps.invoke({
					app,
					name: cfg.requestName.trim() || 'page',
					payload: wire,
				});
				const ms = Math.round(performance.now() - t0);
				deps.patchRuntime(id, { status: 'done', ms });
				deps.log(
					'info',
					`← ${payload.mime} in ${ms} ms (${
						payload.kind === 'text'
							? `${payload.text.length} chars`
							: `${payload.bytes.byteLength} B`
					})`,
					id,
				);
				return { payload };
			}
			case 'prompt': {
				// Prose, so `{{input}}` is substituted verbatim — the JSON escaping that `renderBody`
				// does would print literal \n and \" into a human-readable prompt.
				const text = cfg.promptTemplate.replaceAll(
					'{{input}}',
					input?.kind === 'text' ? input.text : '',
				);
				deps.patchRuntime(id, { status: text.trim() ? 'done' : 'idle' });
				return {
					payload: text.trim() ? { kind: 'text', text, mime: 'text/plain', label: 'prompt' } : null,
				};
			}
			case 'dataset': {
				// SCAFFOLD (see NodeConfig.datasetTable): emits a description of the selection, not
				// rows. It fails loudly rather than emitting a plausible-looking fake, so a flow built
				// on it cannot read as working when it is not.
				const table = cfg.datasetTable.trim();
				if (!table) {
					deps.patchRuntime(id, { status: 'idle' });
					return { payload: null };
				}
				throw new Error(
					`Dataset source is a scaffold — reading rows from ${table} needs the Arrow lane (see NodeConfig.datasetTable).`,
				);
			}
			case 'mcp': {
				// SCAFFOLD, refusing rather than pretending. MCP servers in this repo are Claude Code's
				// SESSION tooling (registered by `make claude-bootstrap`), not a service the app can
				// reach: calling one from a flow needs an MCP client plus a stdio/SSE transport running
				// server-side, which does not exist. The node exists so an agent-shaped flow is
				// expressible on the canvas and the missing piece is named.
				const tool = [cfg.mcpServer.trim(), cfg.mcpTool.trim()].filter(Boolean).join('/');
				throw new Error(
					`MCP tool node is a scaffold — calling ${tool || 'a tool'} needs a server-side MCP client (see NodeConfig.mcpServer).`,
				);
			}
			case 'extract': {
				if (!input) {
					deps.patchRuntime(id, { status: 'idle' });
					return { payload: null };
				}
				if (input.kind !== 'text')
					throw new Error('Extract reads JSON text — wire it after a Model node.');
				const path = cfg.extractPath.trim();
				let parsed: unknown;
				try {
					parsed = JSON.parse(input.text);
				} catch {
					throw new Error('Upstream is not JSON — nothing to extract a path from.');
				}
				const value = path ? pickPath(parsed, path) : parsed;
				if (value === undefined) throw new Error(`No value at "${path}" in the upstream JSON.`);
				const text = renderExtracted(value);
				deps.patchRuntime(id, { status: 'done' });
				deps.log('info', `extracted ${path || '(root)'} → ${text.length} chars`, id);
				return { payload: { kind: 'text', text, mime: 'text/plain', label: path || 'json' } };
			}
			case 'regex': {
				if (!input) {
					deps.patchRuntime(id, { status: 'idle' });
					return { payload: null };
				}
				if (input.kind !== 'text')
					throw new Error('Regex reads text — wire it after a text-producing node.');
				const pattern = cfg.regexPattern.trim();
				if (!pattern) {
					deps.patchRuntime(id, { status: 'idle' });
					return { payload: input };
				}
				// An invalid pattern is a user error with a precise message, not a crashed run.
				let text: string;
				try {
					text = applyRegex(input.text, pattern, cfg.regexMode, cfg.regexReplace);
				} catch (err) {
					throw new Error(`Bad pattern: ${err instanceof Error ? err.message : String(err)}`);
				}
				deps.patchRuntime(id, { status: 'done' });
				return { payload: { kind: 'text', text, mime: 'text/plain', label: cfg.regexMode } };
			}
			case 'compare': {
				// The one MULTI-input node: comparing needs every branch, so it reads all of them
				// rather than the first (see `upstreamPayload`). Two model nodes into one Compare is
				// the A/B the sandbox exists for.
				const texts = predOutputs
					.map((o) => o.payload)
					.filter((p): p is FlowPayload => p !== null)
					.map((p) => (p.kind === 'text' ? p.text : `(${p.mime}, ${p.bytes.byteLength} B)`));
				if (texts.length === 0) {
					deps.patchRuntime(id, { status: 'idle' });
					return { payload: null };
				}
				const text = compareTexts(texts);
				deps.patchRuntime(id, { status: 'done' });
				deps.log('info', `compared ${texts.length} input(s)`, id);
				return { payload: { kind: 'text', text, mime: 'text/plain', label: 'comparison' } };
			}
			case 'alto': {
				if (!input) {
					deps.patchRuntime(id, { status: 'idle' });
					return { payload: null };
				}
				if (input.kind !== 'text')
					throw new Error('ALTO extract reads XML text — wire it after a Model node.');
				const text = altoToText(input.text);
				deps.patchRuntime(id, { status: 'done' });
				return {
					payload: { kind: 'text', text, mime: 'text/plain', label: `${input.label} · text` },
				};
			}
			case 'inspect': {
				// Sink: surface the incoming payload (the component renders it) and
				// forward it unchanged so Inspect can sit mid-chain.
				deps.patchRuntime(id, { status: input ? 'done' : 'idle' });
				return { payload: input };
			}
			default: {
				// Compile-time exhaustiveness: adding a NodeKind without a case errors here.
				const _exhaustive: never = kind;
				return _exhaustive;
			}
		}
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		deps.patchRuntime(id, { status: 'error', error: msg });
		deps.log('error', msg, id);
		return { payload: null, failed: true };
	}
}
