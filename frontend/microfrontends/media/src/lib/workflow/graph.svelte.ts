/**
 * Workflow graph — the shared state for the node-based pipeline editor.
 *
 * A single runes-class singleton (same convention as
 * `$lib/atlas/cross-filter.svelte.ts`) imported by the canvas AND every custom
 * node component, so they all lock-step on one source of truth.
 *
 * Three pieces of state, kept deliberately separate:
 *   • `nodes` / `edges` — the Svelte Flow graph (`$state.raw`, replaced
 *     wholesale; Svelte Flow binds + mutates these on drag/connect/delete).
 *   • `config`  — per-node USER input (query, mode, filter, label…), keyed by id.
 *   • `runtime` — per-node RUN state (status / hits / error / timing), keyed by id.
 *
 * Edges carry STATE forward. Each node produces a `NodeOutput`:
 *   • a partial `SearchSpec` (a query, an image, metadata filters), and/or
 *   • a `Hit[]` result set.
 * `run()` walks the graph in topological order and merges every incoming
 * output into the next node's input. Into a Search, a query/image/filter is
 * WHAT to search for and an upstream result set is WHERE (scopes via
 * `doc_id IN (…)`). A Combine node unions/intersects its incoming result sets.
 *
 * The whole graph (topology + config, minus the un-serialisable image File) is
 * autosaved to localStorage and rehydrated on load.
 */

import type { Edge, Node } from '@xyflow/svelte';
import { browser } from '$app/environment';
import { type Hit, type SearchMode } from '@rask/media-api';
import { nodeFingerprint } from './fingerprint';
import { WorkflowTags } from './tags.svelte';
import { UndoHistory } from './history.svelte';
import { autoLayout } from './layout';
import { runGraph, runSubgraph, type RunDeps } from './executor';
import { dedupeHits } from './scope';
import {
	DEFAULT_N,
	isNodeKind,
	MAX_N,
	MIN_N,
	NODE_KINDS,
	RERANK_TOP_N,
	SEARCH_IMAGE_HANDLE,
	SEARCH_IN_HANDLE,
	type ClipboardNode,
	type CombineMode,
	type NodeConfig,
	type NodeKind,
	type NodeRuntime,
	type RefineScope,
	type RunStatus,
} from './types';
import { safeParseGraph, type PersistedConfig, type PersistedGraph } from './persistence';

// Re-export the public vocabulary so components keep importing it from here.
export { DEFAULT_N, isNodeKind, MAX_N, MIN_N, NODE_KINDS, RERANK_TOP_N };
export type { NodeKind, RunStatus, CombineMode, RefineScope, NodeConfig, NodeRuntime };

const STORAGE_KEY = 'lance-media-workflow-graph-v1';

/** Max undo/redo depth — bounds memory; older snapshots fall off the stack. */
const HISTORY_LIMIT = 50;

/** Pixel offset for a duplicated node, and per successive paste (cascade). */
const DUPLICATE_OFFSET_PX = 48;
const PASTE_OFFSET_PX = 32;

export const SEARCH_MODES: { value: SearchMode; label: string }[] = [
	{ value: 'fts', label: 'Keyword (FTS)' },
	{ value: 'semantic', label: 'Meaning (vector)' },
	{ value: 'hybrid', label: 'Hybrid (FTS + vector)' },
	{ value: 'visual', label: 'Image (frame vector)' },
	{ value: 'scene', label: 'Scene (caption vector)' },
	{ value: 'scene_fts', label: 'Scene (caption keyword)' },
	{ value: 'all', label: 'All judges (RRF)' },
];

export const modeLabel = (mode: SearchMode): string =>
	SEARCH_MODES.find((m) => m.value === mode)?.label ?? mode;

const KIND_LABEL: Record<NodeKind, string> = {
	query: 'Text query',
	image: 'Image',
	filter: 'Filter',
	atlas: 'Atlas selection',
	search: 'Search',
	combine: 'Combine',
	tagger: 'Tagger',
	results: 'Results',
	export: 'Export',
};

export const nodeLabel = (kind: NodeKind): string => KIND_LABEL[kind];

/** Run-status → Tailwind dot colour. Shared by NodeShell and the Inspector. */
export const STATUS_DOT: Record<RunStatus, string> = {
	idle: 'bg-muted-foreground/40',
	running: 'bg-primary animate-pulse',
	done: 'bg-emerald-500',
	error: 'bg-destructive',
};

function defaultConfig(): NodeConfig {
	return {
		q: '',
		image: null,
		imageName: '',
		where: '',
		filters: {},
		mode: 'fts',
		n: DEFAULT_N,
		rerank: false,
		minScore: null,
		refineScope: 'video',
		combineMode: 'union',
		tags: [],
		exportFormat: 'csv',
		exportColumns: null,
		capturedAtlasSelection: null,
		label: '',
		enabled: true,
	};
}

function blankRuntime(): NodeRuntime {
	return {
		status: 'idle',
		error: null,
		hits: null,
		count: null,
		ms: null,
		scopedDocs: null,
		scopedChunks: null,
		scopeCapped: false,
		droppedInputs: 0,
		output: null,
		outputKey: null,
		stale: false,
	};
}

class WorkflowGraph {
	/** The Svelte Flow nodes — bound into <SvelteFlow bind:nodes>. */
	nodes = $state.raw<Node[]>([]);
	/** The Svelte Flow edges — bound into <SvelteFlow bind:edges>. */
	edges = $state.raw<Edge[]>([]);

	/** Per-node user input, keyed by node id. Deep `$state` ON PURPOSE: node
	 *  components mutate fields in place (`bind:value={cfg.q}`,
	 *  `bind:checked={cfg.rerank}`, the Inspector's label bind), and the autosave
	 *  `$effect` tracks those deep writes via `snapshot()`. Must NOT become
	 *  `$state.raw` — that would silently de-reactify every such bind. */
	config = $state<Record<string, NodeConfig>>({});
	/** Per-node run state, keyed by node id. */
	runtime = $state<Record<string, NodeRuntime>>({});

	/** Shared tag store (chunk-identity keyed) — inline tagging in the results list
	 *  and the Tagger node both write here; tags persist + flow into Export. */
	readonly tags = new WorkflowTags();

	/** True while `run()` is in flight (disables the Run button, shows spinner). */
	running = $state(false);
	/** Last graph-level failure (cycle, etc.); per-node errors live in `runtime`. */
	lastError = $state<string | null>(null);

	/** A result the user clicked — plays in the Inspector. */
	selectedHit = $state<Hit | null>(null);

	/** The node whose interatchte state the Inspector shows (click a node). */
	inspectedNodeId = $state<string | null>(null);

	/** Ids currently selected on the canvas (tracked via `onselectionchange`) —
	 *  drives the toolbar's Delete button. */
	selectedNodeIds = $state<string[]>([]);
	selectedEdgeIds = $state<string[]>([]);

	get hasSelection(): boolean {
		return this.selectedNodeIds.length > 0 || this.selectedEdgeIds.length > 0;
	}

	/** Undo/redo stacks (snapshot strings) — its own focused store. */
	private undoHistory = new UndoHistory(HISTORY_LIMIT);
	/** Last snapshot pushed to history, so the debounced checkpoint can diff. */
	private lastCheckpoint = '';
	/** Copy/paste buffer of detached nodes + a paste counter (cascade offset). */
	private clipboard: ClipboardNode[] = [];
	private pasteCount = 0;

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
	 * The starter graph: a multi-modal refinement chain that demonstrates how
	 * to wire "multiples". Upload an image and Run to see all four stages; with
	 * no image the visual stage politely skips and the Scene→Keyword chain still
	 * runs, so the refinement is visible either way.
	 *
	 *   Image ─image─▶ Search·Visual ─refine─▶ Search·Scene ─refine─▶ Search·Keyword ─▶ Results
	 */
	seed(): void {
		const query = (mode: SearchMode, q: string): NodeConfig => ({ ...defaultConfig(), mode, q });
		this.config = {
			image: defaultConfig(),
			'search-visual': query('visual', ''),
			'search-scene': query('scene', 'talarstol'),
			'search-said': query('fts', 'skatt'),
			results: defaultConfig(),
			export: defaultConfig(),
		};
		this.runtime = Object.fromEntries(Object.keys(this.config).map((id) => [id, blankRuntime()]));
		this.tags.reset();
		this.undoHistory.clear();
		this.nodes = [
			{ id: 'image', type: 'image', position: { x: -60, y: 60 }, data: {} },
			{ id: 'search-visual', type: 'search', position: { x: 240, y: 40 }, data: {} },
			{ id: 'search-scene', type: 'search', position: { x: 560, y: 100 }, data: {} },
			{ id: 'search-said', type: 'search', position: { x: 880, y: 160 }, data: {} },
			{ id: 'results', type: 'results', position: { x: 1200, y: 100 }, data: {} },
			{ id: 'export', type: 'export', position: { x: 1520, y: 100 }, data: {} },
		];
		// No `animated` here — edge animation is now run-driven (the canvas pulses
		// edges feeding a running node), so seeding it would just be stripped.
		// Search targets carry an explicit port (its handles all have ids, so an
		// edge without one wouldn't attach).
		this.edges = [
			{
				id: 'e-img',
				source: 'image',
				target: 'search-visual',
				targetHandle: SEARCH_IMAGE_HANDLE,
				label: 'image',
			},
			{
				id: 'e-v-scene',
				source: 'search-visual',
				target: 'search-scene',
				targetHandle: SEARCH_IN_HANDLE,
				label: 'refine',
			},
			{
				id: 'e-scene-said',
				source: 'search-scene',
				target: 'search-said',
				targetHandle: SEARCH_IN_HANDLE,
				label: 'refine',
			},
			{ id: 'e-said-res', source: 'search-said', target: 'results' },
			{ id: 'e-res-exp', source: 'results', target: 'export' },
		];
		this.seq = 0;
		this.running = false;
		this.lastError = null;
		this.selectedHit = null;
		this.inspectedNodeId = null;
		this.selectedNodeIds = [];
		this.selectedEdgeIds = [];
		this.pasteCount = 0;
		this.lastCheckpoint = this.snapshot();
	}

	/** Add a fresh node of `kind` at `position`; returns its id. */
	addNode(kind: NodeKind, position: { x: number; y: number }): string {
		if (this.running) return '';
		const id = `${kind}-${++this.seq}`;
		this.config = { ...this.config, [id]: defaultConfig() };
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
			[newId]: { ...src, image: null, label: src.label ? `${src.label} copy` : '' },
		};
		this.runtime = { ...this.runtime, [newId]: blankRuntime() };
		this.nodes = [...this.nodes, { id: newId, type: kind, position: pos, data: {} }];
		return newId;
	}

	/** Patch one node's user input. `config` is deep `$state`, so components'
	 *  in-place `bind:` mutations are equally reactive — this helper exists for
	 *  multi-field patches and call-site ergonomics (falls back to
	 *  `defaultConfig()` when the id has no config yet). */
	setConfig(id: string, patch: Partial<NodeConfig>): void {
		const prev = this.config[id] ?? defaultConfig();
		this.config = { ...this.config, [id]: { ...prev, ...patch } };
	}

	/** Current fingerprint of a node's output-affecting config + incoming
	 *  edges (see fingerprint.ts) — a mismatch with the stored `outputKey`
	 *  means the node was edited or rewired since its output was recorded. */
	nodeFingerprint(id: string): string {
		return nodeFingerprint(id, this.config[id] ?? defaultConfig(), this.edges);
	}

	/** True when a node's last results were computed from a different config or
	 *  wiring than the current one — drives the "stale" badge live (config is
	 *  deep $state and edges are $state, so reads here re-derive on edit). */
	isOutdated(id: string): boolean {
		const rt = this.runtime[id];
		if (!rt?.output || rt.outputKey === null) return false;
		return rt.outputKey !== this.nodeFingerprint(id);
	}

	// ── Connection validation (keeps the graph a DAG) ───────────────────────────

	/** Validate a would-be edge (wired to `<SvelteFlow isValidConnection>`): no
	 *  self-loop, no duplicate edge, and no edge that would create a cycle (the
	 *  target must not already reach the source). Port direction is enforced by
	 *  the node Handles (sinks expose no source port; sources no target port). */
	canConnect(connection: {
		source: string | null;
		target: string | null;
		targetHandle?: string | null;
	}): boolean {
		return this.connectionError(connection) === null;
	}

	/** Why a would-be connection is invalid (for user feedback), or null if it is
	 *  valid — also the source of truth for `canConnect` and `isValidConnection`
	 *  (which gates edge reconnection too). */
	connectionError(connection: {
		source: string | null;
		target: string | null;
		targetHandle?: string | null;
	}): string | null {
		const { source, target } = connection;
		if (!source || !target) return null; // not over a real target yet
		if (source === target) return "A node can't connect to itself";
		if (this.edges.some((e) => e.source === source && e.target === target))
			return 'Those nodes are already connected';
		if (this.reaches(this.edges, target, source)) return 'That would create a loop';
		// The Search node's image port only takes an Image node — and vice versa,
		// an Image feeding a Search must use that port (the general port would
		// silently merge it anyway, but the wire should show what it carries).
		const targetHandle = connection.targetHandle ?? null;
		const sourceKind = this.kindOf(source);
		if (targetHandle === SEARCH_IMAGE_HANDLE && sourceKind !== 'image')
			return 'Only an Image node can feed the image input';
		if (
			sourceKind === 'image' &&
			this.kindOf(target) === 'search' &&
			targetHandle !== SEARCH_IMAGE_HANDLE
		)
			return "Wire the image into the Search node's image port (lower one)";
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
	 *  before deleting a node that feeds others. */
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

	/** The merged result set flowing INTO `id` from its direct predecessors' LAST
	 *  run — the same union the executor feeds a node's scope. Reads each incoming
	 *  edge's source `runtime.hits` and dedupes by chunk identity. Returns null
	 *  when no predecessor has produced hits (so the Atlas modal shows all points).
	 *  Used at EDIT time (open the Atlas modal pre-filtered to upstream results). */
	getPredecessorHits(id: string): Hit[] | null {
		const merged: Hit[] = [];
		for (const e of this.edges) {
			if (e.target !== id) continue;
			const hits = this.runtime[e.source]?.hits;
			if (hits && hits.length) merged.push(...hits);
		}
		return merged.length ? dedupeHits(merged) : null;
	}

	// ── Undo / redo (auto-checkpointed via the canvas's debounced effect) ────────

	/** Called by the canvas after changes settle (debounced): push the PREVIOUS
	 *  state so the whole settled change — structural OR config edits OR a move —
	 *  becomes one undo step. Nothing is lost between checkpoints. */
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
	 *  string (no-op). Preserves run results for surviving nodes so an unrelated
	 *  structural undo doesn't wipe the displayed hits. */
	private restore(json: string): void {
		const parsed = safeParseGraph(json);
		if (!parsed) return;
		this.applyParsed(parsed, { preserveRuntime: true });
		this.selectedNodeIds = [];
		this.selectedEdgeIds = [];
		this.inspectedNodeId = null;
	}

	// ── Copy / paste + tidy + reconnect ─────────────────────────────────────────

	/** Copy the selected nodes (type + config + position) to the clipboard. */
	copySelection(): void {
		this.clipboard = this.selectedNodeIds.flatMap((id) => {
			const node = this.nodes.find((n) => n.id === id);
			const cfg = this.config[id];
			if (!node || !cfg || !isNodeKind(node.type)) return [];
			return [
				{
					type: node.type,
					config: { ...cfg, image: null },
					position: { ...node.position },
				},
			];
		});
		// A fresh copy starts a fresh paste cascade — don't inherit the old offset.
		this.pasteCount = 0;
	}

	/** Paste the clipboard nodes (fresh ids, cascading offset, selected on the
	 *  canvas). Auto-checkpointed by the settle effect; blocked mid-run. */
	paste(): void {
		if (!this.clipboard.length || this.running) return;
		this.pasteCount += 1;
		const off = PASTE_OFFSET_PX * this.pasteCount; // cascade repeated pastes so they don't stack
		const config = { ...this.config };
		const runtime = { ...this.runtime };
		const newIds = new Set<string>();
		const newNodes: Node[] = [];
		for (const item of this.clipboard) {
			const id = `${item.type}-${++this.seq}`;
			config[id] = { ...item.config, image: null };
			runtime[id] = blankRuntime();
			newNodes.push({
				id,
				type: item.type,
				position: { x: item.position.x + off, y: item.position.y + off },
				data: {},
				selected: true,
			});
			newIds.add(id);
		}
		this.config = config;
		this.runtime = runtime;
		// Deselect existing nodes so only the pasted ones are selected on the canvas.
		this.nodes = [...this.nodes.map((n) => ({ ...n, selected: false })), ...newNodes];
		this.selectedNodeIds = [...newIds];
	}

	/** Auto-layout the graph left-to-right. Blocked mid-run. elkjs is async, so
	 *  the nodes update when layout resolves (fire-and-forget from the button). */
	async tidy(): Promise<void> {
		if (this.running) return;
		this.nodes = await autoLayout(this.nodes, this.edges);
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

	/** Play a clicked result in the Inspector. */
	selectHit(hit: Hit): void {
		this.selectedHit = hit;
	}

	/** Stop playing the selected result (back to the inspected node's results). */
	closeDetail(): void {
		this.selectedHit = null;
	}

	/** Show a node's interatchte state (config + results) in the Inspector.
	 *  Clearing `selectedHit` lets a node click switch the panel away from a
	 *  playing result. (Result-row clicks `stopPropagation`, so they never
	 *  reach here and keep playing.) */
	inspectNode(id: string): void {
		this.selectedHit = null;
		this.inspectedNodeId = id;
	}

	/** Record the canvas selection (from `<SvelteFlow onselectionchange>`). */
	setSelection(nodeIds: string[], edgeIds: string[]): void {
		this.selectedNodeIds = nodeIds;
		this.selectedEdgeIds = edgeIds;
	}

	/** Disconnect one edge (the nodes stay). */
	removeEdge(id: string): void {
		if (this.running) return;
		this.edges = this.edges.filter((e) => e.id !== id);
		this.selectedEdgeIds = this.selectedEdgeIds.filter((x) => x !== id);
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
		this.selectedNodeIds = this.selectedNodeIds.filter((x) => x !== id);
		const liveEdges = new Set(this.edges.map((e) => e.id));
		this.selectedEdgeIds = this.selectedEdgeIds.filter((x) => liveEdges.has(x));
		if (this.inspectedNodeId === id) this.inspectedNodeId = null;
	}

	/** Delete the current selection: selected nodes (and their edges) + edges. */
	deleteSelected(): void {
		const nodeIds = new Set(this.selectedNodeIds);
		const edgeIds = new Set(this.selectedEdgeIds);
		if ((!nodeIds.size && !edgeIds.size) || this.running) return;
		this.nodes = this.nodes.filter((n) => !nodeIds.has(n.id));
		this.edges = this.edges.filter(
			(e) => !edgeIds.has(e.id) && !nodeIds.has(e.source) && !nodeIds.has(e.target),
		);
		const config = { ...this.config };
		const runtime = { ...this.runtime };
		for (const id of nodeIds) {
			delete config[id];
			delete runtime[id];
		}
		this.config = config;
		this.runtime = runtime;
		if (this.inspectedNodeId && nodeIds.has(this.inspectedNodeId)) this.inspectedNodeId = null;
		this.selectedNodeIds = [];
		this.selectedEdgeIds = [];
	}

	/** Prune state for elements Svelte Flow already removed itself — its
	 *  built-in Backspace/Delete mutates the bound `nodes`/`edges` directly, so
	 *  it never reaches `removeNode`/`deleteSelected`. Wired via `<SvelteFlow
	 *  ondelete>` so all three delete paths converge on the same cleanup. */
	syncDeleted(nodeIds: string[], edgeIds: string[]): void {
		if (!nodeIds.length && !edgeIds.length) return;
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
		this.selectedNodeIds = this.selectedNodeIds.filter((x) => !gone.has(x));
		const goneEdges = new Set(edgeIds);
		this.selectedEdgeIds = this.selectedEdgeIds.filter((x) => !goneEdges.has(x));
	}

	// ── Persistence ───────────────────────────────────────────────────────────

	/** JSON snapshot of the serialisable graph. Reads nodes/edges/config deeply,
	 *  so calling it inside a `$effect` tracks every change (drives autosave). */
	snapshot(): string {
		// flatMap + guard: dropping a corrupt node beats emitting it — an invalid
		// `type` would fail the whole PersistedGraphSchema parse on restore.
		const nodes = this.nodes.flatMap((n) =>
			isNodeKind(n.type)
				? [
						{
							id: n.id,
							type: n.type,
							position: { x: n.position.x, y: n.position.y },
						},
					]
				: [],
		);
		// `animated` is intentionally NOT persisted — it's transient run state set by
		// the canvas, so persisting it would churn autosave/history during a run.
		const edges = this.edges.map((e) => ({
			id: e.id,
			source: e.source,
			target: e.target,
			...(typeof e.targetHandle === 'string' ? { targetHandle: e.targetHandle } : {}),
			...(typeof e.label === 'string' ? { label: e.label } : {}),
		}));
		const config: Record<string, PersistedConfig> = {};
		for (const [id, c] of Object.entries(this.config)) {
			config[id] = {
				q: c.q,
				imageName: c.imageName,
				where: c.where,
				filters: c.filters,
				// A generic (any-key) mode narrows to the persisted set here; an unknown
				// key self-heals to 'fts' on reload (persistence.ts picklist fallback).
				mode: c.mode as PersistedConfig['mode'],
				n: c.n,
				rerank: c.rerank,
				minScore: c.minScore,
				refineScope: c.refineScope,
				combineMode: c.combineMode,
				tags: c.tags,
				exportFormat: c.exportFormat,
				exportColumns: c.exportColumns,
				// Never round-trip the captured Atlas selection (heavy Hit[]); see
				// persistence.ts — a reload discards it (re-open the modal to re-select).
				capturedAtlasSelection: null,
				label: c.label,
				enabled: c.enabled,
			};
		}
		return JSON.stringify({
			nodes,
			edges,
			config,
			tags: this.tags.snapshot(),
		} satisfies PersistedGraph);
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

	/** Rehydrate from localStorage; returns false (→ caller seeds) if absent/bad.
	 *  A bad node shape / unknown kind fails the parse, so we seed instead of
	 *  crashing the canvas. */
	private load(): boolean {
		if (!browser) return false;
		const parsed = safeParseGraph(localStorage.getItem(STORAGE_KEY));
		if (!parsed) return false;
		this.applyParsed(parsed);
		return true;
	}

	/** Rebuild nodes/edges/config/runtime/tags from a parsed graph — shared by
	 *  load() (localStorage) and restore() (undo/redo). `preserveRuntime` keeps
	 *  existing run results for surviving node ids (undo/redo shouldn't wipe hits). */
	private applyParsed(parsed: PersistedGraph, opts: { preserveRuntime?: boolean } = {}): void {
		const nodes: Node[] = parsed.nodes.map((n) => ({
			id: n.id,
			type: n.type,
			position: { x: n.position.x, y: n.position.y },
			data: {},
		}));
		const ids = new Set(nodes.map((n) => n.id));
		const kindById = new Map(parsed.nodes.map((n) => [n.id, n.type]));
		// A Search target's port: the stored handle, or (pre-two-port snapshots)
		// inferred from the source — an Image feeds the image port, all else "in".
		// Without one the edge wouldn't attach: every Search handle has an id.
		const searchPort = (e: { source: string; targetHandle?: string | undefined }): string =>
			e.targetHandle ??
			(kindById.get(e.source) === 'image' ? SEARCH_IMAGE_HANDLE : SEARCH_IN_HANDLE);
		const edges: Edge[] = parsed.edges
			.filter((e) => ids.has(e.source) && ids.has(e.target)) // drop dangling edges
			.map((e) => ({
				id: e.id,
				source: e.source,
				target: e.target,
				...(kindById.get(e.target) === 'search' ? { targetHandle: searchPort(e) } : {}),
				...(e.label ? { label: e.label } : {}),
			}));
		const config: Record<string, NodeConfig> = {};
		const runtime: Record<string, NodeRuntime> = {};
		for (const n of nodes) {
			const pc = parsed.config[n.id];
			config[n.id] = pc ? { ...pc, image: null } : defaultConfig();
			runtime[n.id] = opts.preserveRuntime
				? (this.runtime[n.id] ?? blankRuntime())
				: blankRuntime();
		}
		this.nodes = nodes;
		this.edges = edges;
		this.config = config;
		this.runtime = runtime;
		this.tags.hydrate(parsed.tags);
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

	// ── Execution (delegated to executor.ts) ────────────────────────────────

	/** The executor's narrow view of our state + the runtime writers it needs.
	 *  The executor owns the dataflow algorithm; the store owns the state. */
	private runDeps(): RunDeps {
		return {
			nodes: this.nodes,
			edges: this.edges,
			config: (id) => this.config[id] ?? defaultConfig(),
			kindOf: (id) => this.kindOf(id),
			patchRuntime: (id, patch) => this.patchRuntime(id, patch),
			tagHits: (hits, tags) => this.tags.addTo(hits, tags),
			cachedOutput: (id) => {
				const rt = this.runtime[id];
				const out = rt?.output;
				if (!out || out.failed || rt.stale) return null;
				// Edited or rewired since this output was recorded (covers the
				// enabled toggle too — it's part of the fingerprint).
				if (rt.outputKey !== this.nodeFingerprint(id)) return null;
				return out;
			},
			fingerprint: (id) => this.nodeFingerprint(id),
		};
	}

	/** Run the whole graph from scratch (the toolbar Run button). */
	async run(): Promise<void> {
		if (this.running) return;
		this.running = true;
		this.resetRun();
		try {
			const error = await runGraph(this.runDeps());
			if (error) this.lastError = error;
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
		try {
			const { error, ran } = await runSubgraph(this.runDeps(), id, opts);
			if (error) {
				this.lastError = error;
				return;
			}
			// Flag dependents that did NOT run: their shown results were computed
			// from outputs that just changed. (Stale spreads later runs too — a
			// stale cache is never reused, see cachedOutput.)
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
export const graph = new WorkflowGraph();
