/**
 * Flow graph — the shared state for studio's node-based online sandbox.
 *
 * A single runes-class singleton imported by the canvas AND every custom node
 * component, so they all lock-step on one source of truth. Same architecture as
 * the explorer workflow editor (the estate's reference flow editor), with the
 * domain swapped: edges carry a concrete `FlowPayload` (image bytes / text) and
 * a Model node POSTs it to a live Ray Serve app.
 *
 * Three pieces of state, kept deliberately separate:
 *   • `nodes` / `edges` — the Svelte Flow graph (`$state.raw`, replaced
 *     wholesale; Svelte Flow binds + mutates these on drag/connect/delete).
 *   • `config`  — per-node USER input (file, text, app…), keyed by id. Deep
 *     `$state` ON PURPOSE: node components mutate fields in place
 *     (`bind:value={cfg.text}`) and the autosave `$effect` tracks those deep
 *     writes via `snapshot()`. Must NOT become `$state.raw`.
 *   • `runtime` — per-node RUN state (status / output / error / timing).
 *
 * The whole graph (topology + config, minus the un-serialisable image File) is
 * autosaved to localStorage and rehydrated on load. This module is only ever
 * evaluated client-side (the zone root `/studio` sets `ssr = false` — deliberately,
 * the reference editor's known drift is that its "client-only" singleton
 * actually SSRs and is shared across requests).
 */
import type { Edge, Node } from '@xyflow/svelte';
import { browser } from '$app/environment';
import { depths, layout } from '@rask/flow';
import { nodeFingerprint } from './fingerprint';
import { UndoHistory } from './history.svelte';
import { runGraph, runSubgraph, type RunDeps } from './executor';
import { invokeServe } from './invoke';
import { logs } from './logs.svelte';
import {
	DEFAULT_JSON_BODY,
	DEFAULT_PROMPT,
	isNodeKind,
	NODE_KINDS,
	type NodeConfig,
	type NodeKind,
	type NodeRuntime,
	type RunStatus,
} from './types';
import {
	safeParseGraph,
	STORAGE_KEY,
	type PersistedConfig,
	type PersistedGraph,
} from './persistence';

// Re-export the public vocabulary so components keep importing it from here.
export { isNodeKind, NODE_KINDS };
export type { NodeConfig, NodeKind, NodeRuntime, RunStatus };

/** Max undo/redo depth — bounds memory; older snapshots fall off the stack. */
const HISTORY_LIMIT = 50;

/** Pixel offset for a duplicated node. */
const DUPLICATE_OFFSET_PX = 48;

/** Auto-arrange: @rask/flow's placer uses a viewer pitch (240×110); flow cards
 *  are wider/taller (w-64 + fields), so stretch to keep columns clear. */
const TIDY_STRETCH_X = 1.5;
const TIDY_STRETCH_Y = 2;

const KIND_LABEL: Record<NodeKind, string> = {
	image: 'Image',
	text: 'Text',
	dataset: 'Lakehouse table',
	prompt: 'Prompt',
	model: 'Model endpoint',
	mcp: 'MCP tool',
	alto: 'ALTO → text',
	extract: 'Extract JSON',
	regex: 'Regex',
	compare: 'Compare',
	inspect: 'Inspect',
};

export const nodeLabel = (kind: NodeKind): string => KIND_LABEL[kind];

/** Run-status → Tailwind dot colour. Shared by NodeShell and the toolbar. */
export const STATUS_DOT: Record<RunStatus, string> = {
	idle: 'bg-muted-foreground/40',
	running: 'bg-primary animate-pulse',
	done: 'bg-emerald-500',
	error: 'bg-destructive',
};

/** Node kinds that take a SINGLE input in v0 — the connection validation
 *  refuses a second incoming edge (multi-input merge is a later concept). */
const SINGLE_INPUT_KINDS: ReadonlySet<NodeKind> = new Set([
	'model',
	'mcp',
	'alto',
	'extract',
	'regex',
	'inspect',
]);
// `compare` is deliberately ABSENT: comparing one thing is not comparing. It is the only node that
// reads every incoming payload rather than the first.

function defaultConfig(): NodeConfig {
	return {
		label: '',
		enabled: true,
		file: null,
		fileName: '',
		text: '',
		app: 'htrflow',
		requestName: '',
		modelPath: '',
		bodyMode: 'upstream',
		bodyTemplate: DEFAULT_JSON_BODY,
		promptTemplate: DEFAULT_PROMPT,
		datasetTable: '',
		mcpServer: '',
		mcpTool: '',
		mcpArgs: '{}',
		extractPath: '',
		regexPattern: '',
		regexReplace: '',
		regexMode: 'extract',
	};
}

function blankRuntime(): NodeRuntime {
	return {
		status: 'idle',
		error: null,
		ms: null,
		output: null,
		outputKey: null,
		stale: false,
	};
}

class FlowsGraph {
	/** The Svelte Flow nodes — bound into <SvelteFlow bind:nodes>. */
	nodes = $state.raw<Node[]>([]);
	/** The Svelte Flow edges — bound into <SvelteFlow bind:edges>. */
	edges = $state.raw<Edge[]>([]);

	/** Per-node user input, keyed by node id (deep `$state`, see module doc). */
	config = $state<Record<string, NodeConfig>>({});
	/** Per-node run state, keyed by node id. */
	runtime = $state<Record<string, NodeRuntime>>({});

	/** True while a run is in flight (disables Run, shows spinner). */
	running = $state(false);
	/** Last graph-level failure (cycle, …); per-node errors live in `runtime`. */
	lastError = $state<string | null>(null);

	/** The node the right-hand inspector describes (set by clicking a node). Kept here rather than
	 *  in the page so the canvas, the node cards and the inspector cannot disagree about which node
	 *  is being looked at. */
	inspectedNodeId = $state<string | null>(null);

	/** Show a node in the inspector. */
	inspectNode(id: string): void {
		this.inspectedNodeId = id;
	}

	/** Undo/redo stacks (snapshot strings) — its own focused store. */
	private undoHistory = new UndoHistory(HISTORY_LIMIT);
	/** Last snapshot pushed to history, so the debounced checkpoint can diff. */
	private lastCheckpoint = '';

	get canUndo(): boolean {
		return this.undoHistory.canUndo;
	}
	get canRedo(): boolean {
		return this.undoHistory.canRedo;
	}

	/** Monotonic id source for nodes added at runtime (seeds use bare-kind ids). */
	private seq = 0;

	constructor() {
		if (!this.load()) this.seed();
	}

	/** Kind of a node by id (its Svelte Flow `type`). */
	kindOf(id: string): NodeKind | null {
		const t = this.nodes.find((x) => x.id === id)?.type;
		return isNodeKind(t) ? t : null;
	}

	/**
	 * The starter graph — the estate's own HTR chain, end to end:
	 *
	 *   Image ─▶ Model (/htrflow) ─▶ ALTO → text ─▶ Inspect
	 *
	 * Upload a page scan and Run: the Model node POSTs it to the live Serve
	 * deployment and the chain shows the ALTO XML turned into plain text.
	 */
	seed(): void {
		this.config = {
			image: defaultConfig(),
			model: defaultConfig(),
			alto: defaultConfig(),
			inspect: defaultConfig(),
		};
		this.runtime = Object.fromEntries(Object.keys(this.config).map((id) => [id, blankRuntime()]));
		this.undoHistory.clear();
		this.nodes = [
			{ id: 'image', type: 'image', position: { x: -40, y: 60 }, data: {} },
			{ id: 'model', type: 'model', position: { x: 280, y: 60 }, data: {} },
			{ id: 'alto', type: 'alto', position: { x: 620, y: 60 }, data: {} },
			{ id: 'inspect', type: 'inspect', position: { x: 940, y: 60 }, data: {} },
		];
		this.edges = [
			{ id: 'e-img-model', source: 'image', target: 'model' },
			{ id: 'e-model-alto', source: 'model', target: 'alto' },
			{ id: 'e-alto-inspect', source: 'alto', target: 'inspect' },
		];
		this.seq = 0;
		this.running = false;
		this.lastError = null;
		this.lastCheckpoint = this.snapshot();
	}

	/** Add a fresh node of `kind` at `position`; returns its id. `preset` lands ON TOP of the
	 *  defaults, which is how a live Ray Serve row in the node library drops a Model node already
	 *  pointed at its app instead of a blank one the user has to configure. */
	addNode(
		kind: NodeKind,
		position: { x: number; y: number },
		preset?: Partial<NodeConfig>,
	): string {
		if (this.running) return '';
		const id = `${kind}-${++this.seq}`;
		this.config = { ...this.config, [id]: { ...defaultConfig(), ...preset } };
		this.runtime = { ...this.runtime, [id]: blankRuntime() };
		this.nodes = [...this.nodes, { id, type: kind, position, data: {} }];
		return id;
	}

	/** Duplicate a node (its config, sans the image File) at a small offset. */
	duplicateNode(id: string): string {
		const src = this.config[id];
		const kind = this.kindOf(id);
		if (!src || !kind || this.running) return id;
		const newId = `${kind}-${++this.seq}`;
		const srcNode = this.nodes.find((n) => n.id === id);
		const pos = srcNode
			? { x: srcNode.position.x + DUPLICATE_OFFSET_PX, y: srcNode.position.y + DUPLICATE_OFFSET_PX }
			: { x: 0, y: 0 };
		this.config = {
			...this.config,
			[newId]: { ...src, file: null, label: src.label ? `${src.label} copy` : '' },
		};
		this.runtime = { ...this.runtime, [newId]: blankRuntime() };
		this.nodes = [...this.nodes, { id: newId, type: kind, position: pos, data: {} }];
		return newId;
	}

	/** Patch one node's user input (multi-field patches + call-site ergonomics;
	 *  simple fields also `bind:` straight into the deep `$state` config). */
	setConfig(id: string, patch: Partial<NodeConfig>): void {
		const prev = this.config[id] ?? defaultConfig();
		this.config = { ...this.config, [id]: { ...prev, ...patch } };
	}

	/** Current fingerprint of a node's output-affecting config + incoming edges. */
	nodeFingerprint(id: string): string {
		return nodeFingerprint(id, this.config[id] ?? defaultConfig(), this.edges);
	}

	/** True when a node's last results were computed from a different config or
	 *  wiring than the current one — drives the "stale" badge live. */
	isOutdated(id: string): boolean {
		const rt = this.runtime[id];
		if (!rt?.output || rt.outputKey === null) return false;
		return rt.outputKey !== this.nodeFingerprint(id);
	}

	// ── Connection validation (keeps the graph a DAG) ─────────────────────────

	/** Why a would-be connection is invalid (for user feedback), or null if it
	 *  is valid — wired to `<SvelteFlow isValidConnection>` (which also gates
	 *  edge reconnection). Port direction is enforced by the node Handles
	 *  (sinks expose no source port; sources no target port). */
	connectionError(connection: { source: string | null; target: string | null }): string | null {
		const { source, target } = connection;
		if (!source || !target) return null; // not over a real target yet
		if (source === target) return "A node can't connect to itself";
		if (this.edges.some((e) => e.source === source && e.target === target))
			return 'Those nodes are already connected';
		if (this.reaches(this.edges, target, source)) return 'That would create a loop';
		const targetKind = this.kindOf(target);
		if (
			targetKind &&
			SINGLE_INPUT_KINDS.has(targetKind) &&
			this.edges.some((e) => e.target === target)
		)
			return `${nodeLabel(targetKind)} takes one input — disconnect the existing edge first`;
		if (this.kindOf(source) === 'image' && targetKind === 'alto')
			return 'ALTO extract reads XML text — wire the image through a Model first';
		return null;
	}

	/** source → [targets] adjacency over `edges` (shared by the graph walks). */
	private adjacency(edges: Edge[]): Map<string, string[]> {
		const adj = new Map<string, string[]>();
		for (const e of edges) {
			const list = adj.get(e.source);
			if (list) list.push(e.target);
			else adj.set(e.source, [e.target]);
		}
		return adj;
	}

	/** True if `from` can reach `to` by following `edges` (cycle guard). */
	private reaches(edges: Edge[], from: string, to: string): boolean {
		const adj = this.adjacency(edges);
		const stack = [from];
		const seen = new Set<string>();
		while (stack.length) {
			const id = stack.pop()!;
			if (id === to) return true;
			if (seen.has(id)) continue;
			seen.add(id);
			for (const next of adj.get(id) ?? []) stack.push(next);
		}
		return false;
	}

	/** Node ids reachable DOWNSTREAM of `id` (its dependents) — used to confirm
	 *  before deleting a node that feeds others, and for stale propagation. */
	dependentsOf(id: string): string[] {
		const adj = this.adjacency(this.edges);
		const out = new Set<string>();
		const stack = [...(adj.get(id) ?? [])];
		while (stack.length) {
			const next = stack.pop()!;
			if (out.has(next)) continue;
			out.add(next);
			for (const n of adj.get(next) ?? []) stack.push(n);
		}
		out.delete(id);
		return [...out];
	}

	// ── Undo / redo (auto-checkpointed via the canvas's debounced effect) ─────

	/** Called by the canvas after changes settle (debounced): push the PREVIOUS
	 *  state so the whole settled change becomes one undo step. */
	checkpoint(json: string): void {
		if (this.lastCheckpoint === '') {
			this.lastCheckpoint = json; // first call establishes the baseline
			return;
		}
		if (json === this.lastCheckpoint) return;
		this.undoHistory.push(this.lastCheckpoint);
		this.lastCheckpoint = json;
	}

	undo(): void {
		if (this.running) return;
		const prev = this.undoHistory.undo(this.snapshot());
		if (prev === null) return;
		this.restore(prev);
		this.lastCheckpoint = prev; // settle-checkpoint then sees no change
	}

	redo(): void {
		if (this.running) return;
		const next = this.undoHistory.redo(this.snapshot());
		if (next === null) return;
		this.restore(next);
		this.lastCheckpoint = next;
	}

	/** Rebuild the graph from a snapshot string (undo/redo). Tolerant of a bad
	 *  string (no-op). Preserves run results for surviving nodes. */
	private restore(json: string): void {
		const parsed = safeParseGraph(json);
		if (!parsed) return;
		this.applyParsed(parsed, { preserveRuntime: true });
	}

	/** Auto-arrange the graph left-to-right with @rask/flow's layered placer.
	 *  The placer speaks the lineage edge direction (source = downstream), so
	 *  dataflow edges are inverted before computing depths. */
	tidy(): void {
		if (this.running) return;
		const ids = this.nodes.map((n) => n.id);
		const inverted = this.edges.map((e) => ({ source: e.target, target: e.source }));
		const depthOf = depths(ids, inverted);
		const placed = layout(ids, inverted, (id) => depthOf.get(id) ?? 0);
		this.nodes = this.nodes.map((n) => {
			const p = placed.get(n.id);
			return p ? { ...n, position: { x: p.x * TIDY_STRETCH_X, y: p.y * TIDY_STRETCH_Y } } : n;
		});
	}

	/** Patch one node's run state. */
	private patchRuntime(id: string, patch: Partial<NodeRuntime>): void {
		const prev = this.runtime[id] ?? blankRuntime();
		this.runtime = { ...this.runtime, [id]: { ...prev, ...patch } };
	}

	/** Clear all run state back to idle (keeps the graph + user input). */
	resetRun(): void {
		const next: Record<string, NodeRuntime> = {};
		for (const n of this.nodes) next[n.id] = blankRuntime();
		this.runtime = next;
		this.lastError = null;
	}

	/** Reset everything to the seeded starter graph. */
	reset(): void {
		this.seed();
	}

	/** Replace the whole graph from a saved snapshot (the flow library). Returns false when the
	 *  string does not parse, so the caller can say so rather than silently doing nothing. Run
	 *  results are NOT preserved: this is a different flow, and keeping the previous flow's outputs
	 *  on same-named nodes would be a lie. */
	loadSnapshot(json: string): boolean {
		if (this.running) return false;
		const parsed = safeParseGraph(json);
		if (!parsed) return false;
		this.applyParsed(parsed);
		this.inspectedNodeId = null;
		this.lastError = null;
		this.lastCheckpoint = this.snapshot();
		return true;
	}

	/** Disconnect one edge (the nodes stay). */
	removeEdge(id: string): void {
		if (this.running) return;
		this.edges = this.edges.filter((e) => e.id !== id);
	}

	/** Delete one node, every edge touching it, and its config/runtime. */
	removeNode(id: string): void {
		if (this.running) return;
		this.nodes = this.nodes.filter((n) => n.id !== id);
		this.edges = this.edges.filter((e) => e.source !== id && e.target !== id);
		const config = { ...this.config };
		const runtime = { ...this.runtime };
		delete config[id];
		delete runtime[id];
		this.config = config;
		this.runtime = runtime;
		if (this.inspectedNodeId === id) this.inspectedNodeId = null;
	}

	/** Prune state for elements Svelte Flow already removed itself — its
	 *  built-in Backspace/Delete mutates the bound `nodes`/`edges` directly, so
	 *  it never reaches `removeNode`. Wired via `<SvelteFlow ondelete>` so both
	 *  delete paths converge on the same cleanup. */
	syncDeleted(nodeIds: string[]): void {
		if (!nodeIds.length) return;
		const gone = new Set(nodeIds);
		const config = { ...this.config };
		const runtime = { ...this.runtime };
		for (const id of gone) {
			delete config[id];
			delete runtime[id];
		}
		this.config = config;
		this.runtime = runtime;
		if (this.inspectedNodeId && gone.has(this.inspectedNodeId)) this.inspectedNodeId = null;
	}

	// ── Persistence ───────────────────────────────────────────────────────────

	/** JSON snapshot of the serialisable graph. Reads nodes/edges/config deeply,
	 *  so calling it inside a `$effect` tracks every change (drives autosave). */
	snapshot(): string {
		// flatMap + guard: dropping a corrupt node beats emitting it — an invalid
		// `type` would fail the whole PersistedGraphSchema parse on restore.
		const nodes = this.nodes.flatMap((n) =>
			isNodeKind(n.type)
				? [{ id: n.id, type: n.type, position: { x: n.position.x, y: n.position.y } }]
				: [],
		);
		const edges = this.edges.map((e) => ({ id: e.id, source: e.source, target: e.target }));
		const config: Record<string, PersistedConfig> = {};
		for (const [id, c] of Object.entries(this.config)) {
			config[id] = {
				label: c.label,
				enabled: c.enabled,
				fileName: c.fileName,
				text: c.text,
				app: c.app,
				requestName: c.requestName,
				modelPath: c.modelPath,
				bodyMode: c.bodyMode,
				bodyTemplate: c.bodyTemplate,
				promptTemplate: c.promptTemplate,
				datasetTable: c.datasetTable,
				mcpServer: c.mcpServer,
				mcpTool: c.mcpTool,
				mcpArgs: c.mcpArgs,
				extractPath: c.extractPath,
				regexPattern: c.regexPattern,
				regexReplace: c.regexReplace,
				regexMode: c.regexMode,
			};
		}
		return JSON.stringify({ nodes, edges, config } satisfies PersistedGraph);
	}

	/** Write a snapshot string to localStorage (no-op outside the browser). */
	persist(json: string): void {
		if (!browser) return;
		try {
			localStorage.setItem(STORAGE_KEY, json);
		} catch {
			// storage full / disabled — autosave is best-effort, never throw.
		}
	}

	/** Rehydrate from localStorage; returns false (→ caller seeds) if absent/bad. */
	private load(): boolean {
		if (!browser) return false;
		const parsed = safeParseGraph(localStorage.getItem(STORAGE_KEY));
		if (!parsed) return false;
		this.applyParsed(parsed);
		return true;
	}

	/** Rebuild nodes/edges/config/runtime from a parsed graph — shared by
	 *  load() (localStorage) and restore() (undo/redo). `preserveRuntime` keeps
	 *  existing run results for surviving node ids. */
	private applyParsed(parsed: PersistedGraph, opts: { preserveRuntime?: boolean } = {}): void {
		const nodes: Node[] = parsed.nodes.map((n) => ({
			id: n.id,
			type: n.type,
			position: { x: n.position.x, y: n.position.y },
			data: {},
		}));
		const ids = new Set(nodes.map((n) => n.id));
		const edges: Edge[] = parsed.edges
			.filter((e) => ids.has(e.source) && ids.has(e.target)) // drop dangling edges
			.map((e) => ({ id: e.id, source: e.source, target: e.target }));
		const config: Record<string, NodeConfig> = {};
		const runtime: Record<string, NodeRuntime> = {};
		for (const n of nodes) {
			const pc = parsed.config[n.id];
			config[n.id] = pc ? { ...pc, file: null } : defaultConfig();
			runtime[n.id] = opts.preserveRuntime
				? (this.runtime[n.id] ?? blankRuntime())
				: blankRuntime();
		}
		this.nodes = nodes;
		this.edges = edges;
		this.config = config;
		this.runtime = runtime;
		this.seq = this.maxSeq();
	}

	/** Highest numeric suffix across node ids (so new ids never collide). */
	private maxSeq(): number {
		let max = 0;
		for (const n of this.nodes) {
			const m = /-(\d+)$/.exec(n.id);
			if (m) max = Math.max(max, Number(m[1]));
		}
		return max;
	}

	// ── Execution (delegated to executor.ts) ──────────────────────────────────

	/** The executor's narrow view of our state + the runtime writers it needs.
	 *  The executor owns the dataflow algorithm; the store owns the state. */
	private runDeps(): RunDeps {
		return {
			nodes: this.nodes,
			edges: this.edges,
			config: (id) => this.config[id] ?? defaultConfig(),
			kindOf: (id) => this.kindOf(id),
			patchRuntime: (id, patch) => this.patchRuntime(id, patch),
			cachedOutput: (id) => {
				const rt = this.runtime[id];
				const out = rt?.output;
				if (!out || out.failed || rt.stale) return null;
				if (rt.outputKey !== this.nodeFingerprint(id)) return null;
				return out;
			},
			fingerprint: (id) => this.nodeFingerprint(id),
			invoke: invokeServe,
			log: (level, message, nodeId) => logs.push(level, message, nodeId ?? null),
		};
	}

	/** Run the whole graph from scratch (the toolbar Run button). */
	async run(): Promise<void> {
		if (this.running) return;
		this.running = true;
		this.resetRun();
		logs.push('info', `run started — ${this.nodes.length} node(s)`);
		const t0 = performance.now();
		try {
			const error = await runGraph(this.runDeps());
			if (error) {
				this.lastError = error;
				logs.push('error', error);
			} else {
				logs.push('info', `run finished in ${Math.round(performance.now() - t0)} ms`);
			}
		} finally {
			this.running = false;
		}
	}

	/** Run ONE node (the node ▶ button): recomputes the node itself, reuses
	 *  upstream results where they exist, and computes missing/stale/failed
	 *  upstream once. `fresh` re-executes the whole upstream branch. Everything
	 *  else keeps its results but is flagged stale when it now sits downstream
	 *  of fresher data. */
	async runNode(id: string, opts: { fresh?: boolean } = {}): Promise<void> {
		if (this.running) return;
		this.running = true;
		this.lastError = null;
		logs.push('info', opts.fresh ? 'run node (fresh branch)' : 'run node', id);
		try {
			const { error, ran } = await runSubgraph(this.runDeps(), id, opts);
			if (error) {
				this.lastError = error;
				logs.push('error', error);
				return;
			}
			// Which nodes actually executed vs came from cache is THE thing to know when a per-node run
			// looks like it did nothing.
			logs.push('info', `ran ${ran.length}: ${ran.join(', ')} (others reused)`, id);
			// Flag dependents that did NOT run: their shown results were computed
			// from outputs that just changed.
			const ranSet = new Set(ran);
			const staleIds = new Set<string>();
			for (const r of ran) {
				for (const d of this.dependentsOf(r)) if (!ranSet.has(d)) staleIds.add(d);
			}
			for (const d of staleIds) {
				const rt = this.runtime[d];
				if (rt && (rt.status !== 'idle' || rt.output)) this.patchRuntime(d, { stale: true });
			}
		} finally {
			this.running = false;
		}
	}
}

/** The singleton, imported by the canvas and every node component. */
export const graph = new FlowsGraph();
