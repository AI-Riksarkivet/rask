<script lang="ts">
	/** The Svelte Flow pane + all editor interactions. Lives INSIDE a
	 *  <SvelteFlowProvider> (see WorkflowCanvas) so `useSvelteFlow()` works for
	 *  drag-drop and pane context-menu positioning. Wires: arrowheads, snap-grid,
	 *  edge reconnection, invalid-connection feedback, run-edge animation,
	 *  undo/redo + copy/paste keyboard, a drag palette, and a context menu. */
	import {
		SvelteFlow,
		Background,
		Controls,
		MiniMap,
		Panel,
		useSvelteFlow,
		type Connection,
		type Edge,
		type EdgeTypes,
	} from '@xyflow/svelte';
	import { useColorMode } from '@rask/ui/color-mode';
	import { graph, isNodeKind } from '$lib/workflow/graph.svelte';
	import { nodeTypes } from '$lib/workflow/node-types';
	import { ARROW_MARKER } from '$lib/workflow/edges';
	import ReconnectableEdge from '$lib/workflow/ReconnectableEdge.svelte';
	import WorkflowToolbar from '$lib/workflow/WorkflowToolbar.svelte';
	import NodePalette, { NODE_DND_MIME } from '$lib/workflow/NodePalette.svelte';
	import ContextMenu from '$lib/workflow/ContextMenu.svelte';

	const { screenToFlowPosition } = useSvelteFlow();

	// Custom default edge: renders the bezier path + reconnect anchors so dragging
	// an endpoint rewires the edge (validity gated by isValidConnection).
	const edgeTypes: EdgeTypes = { default: ReconnectableEdge };

	/** Debounce before autosave + history checkpoint (one step per settled burst). */
	const AUTOSAVE_DEBOUNCE_MS = 400;
	/** How long an invalid-connection toast stays up. */
	const INVALID_TOAST_MS = 2600;
	/** Canvas snap-to-grid size (px). */
	const SNAP_GRID_PX = 16;

	// Match the app's theme (boots dark; .dark class on <html>) — reactive, so the
	// flow's colours follow the runtime theme toggle instead of going stale.
	const theme = useColorMode();

	// ── Autosave (snapshot reads deeply → effect re-runs on any change) ──────────
	$effect(() => {
		const json = graph.snapshot();
		const timer = setTimeout(() => {
			graph.persist(json);
			graph.checkpoint(json); // settled change → one undo step (structural OR config)
		}, AUTOSAVE_DEBOUNCE_MS);
		return () => clearTimeout(timer);
	});

	// Edge arrowheads/colour/label + the run pulse are derived inside
	// ReconnectableEdge — no effect mutates edge state here.

	// ── Connection validation + invalid feedback ─────────────────────────────────
	let invalidMsg = $state<string | null>(null);
	let pendingReason: string | null = null;
	let madeConnection = false;
	let msgTimer: ReturnType<typeof setTimeout> | null = null;

	function flash(msg: string): void {
		invalidMsg = msg;
		if (msgTimer) clearTimeout(msgTimer);
		msgTimer = setTimeout(() => (invalidMsg = null), INVALID_TOAST_MS);
	}

	// Don't let a pending toast timer outlive the component (same standard as the
	// autosave effect's cleanup above).
	$effect(() => () => {
		if (msgTimer) clearTimeout(msgTimer);
	});

	function validate(connection: Connection | Edge): boolean {
		const reason = graph.connectionError(connection);
		if (reason && connection.source && connection.target) pendingReason = reason;
		return reason === null;
	}

	// ── Context menu ─────────────────────────────────────────────────────────────
	interface MenuState {
		x: number;
		y: number;
		mode: 'node' | 'pane';
		nodeId: string | null;
		flow: { x: number; y: number };
	}
	let menu = $state<MenuState | null>(null);

	// ── Drag-from-palette drop ───────────────────────────────────────────────────
	function onDrop(e: DragEvent): void {
		e.preventDefault();
		const kind = e.dataTransfer?.getData(NODE_DND_MIME);
		// getData's '' falsy case is covered — '' is not in NODE_KINDS.
		if (!isNodeKind(kind)) return;
		graph.addNode(kind, screenToFlowPosition({ x: e.clientX, y: e.clientY }));
	}
	function onDragOver(e: DragEvent): void {
		e.preventDefault();
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
	}

	// ── Keyboard: undo/redo + copy/paste (ignored while typing) ──────────────────
	function isTyping(t: EventTarget | null): boolean {
		if (!(t instanceof HTMLElement)) return false;
		return (
			t.tagName === 'INPUT' ||
			t.tagName === 'TEXTAREA' ||
			t.tagName === 'SELECT' ||
			t.isContentEditable
		);
	}
	function onKeydown(e: KeyboardEvent): void {
		if (!(e.ctrlKey || e.metaKey) || isTyping(e.target)) return;
		const k = e.key.toLowerCase();
		if (k === 'z' && !e.shiftKey) {
			e.preventDefault();
			graph.undo();
		} else if ((k === 'z' && e.shiftKey) || k === 'y') {
			e.preventDefault();
			graph.redo();
		} else if (k === 'c') {
			graph.copySelection();
		} else if (k === 'v') {
			graph.paste();
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="h-full w-full" ondragover={onDragOver} ondrop={onDrop}>
	<SvelteFlow
		bind:nodes={graph.nodes}
		bind:edges={graph.edges}
		{nodeTypes}
		{edgeTypes}
		colorMode={theme.current}
		fitView
		deleteKey={['Backspace', 'Delete']}
		snapGrid={[SNAP_GRID_PX, SNAP_GRID_PX]}
		defaultEdgeOptions={{ markerEnd: ARROW_MARKER }}
		isValidConnection={validate}
		onnodeclick={(e) => graph.inspectNode(e.node.id)}
		onnodecontextmenu={({ node, event }) => {
	event.preventDefault();
	menu = {
		x: event.clientX,
		y: event.clientY,
		mode: 'node',
		nodeId: node.id,
		flow: { x: 0, y: 0 },
	};
}}
		onpanecontextmenu={({ event }) => {
	event.preventDefault();
	menu = {
		x: event.clientX,
		y: event.clientY,
		mode: 'pane',
		nodeId: null,
		flow: screenToFlowPosition({ x: event.clientX, y: event.clientY }),
	};
}}
		onconnectstart={() => {
	pendingReason = null;
	madeConnection = false;
	invalidMsg = null;
}}
		onconnect={() => {
	madeConnection = true;
	pendingReason = null;
}}
		onconnectend={(_event, connectionState) => {
	// Only flash the reason if the drag actually ended over an (invalid)
	// handle — releasing over empty pane is a legitimate cancel, stay silent.
	if (!madeConnection && pendingReason && connectionState?.toHandle) flash(pendingReason);
}}
		onbeforedelete={async ({ nodes }) => {
	const feedsOthers = nodes.some((n) => graph.dependentsOf(n.id).length > 0);
	return !feedsOthers || window.confirm('Delete node(s) that feed others downstream?');
}}
		ondelete={({ nodes, edges }) =>
	graph.syncDeleted(
		nodes.map((n) => n.id),
		edges.map((e) => e.id),
	)}
		onselectionchange={(p) =>
	graph.setSelection(
		p.nodes.map((n) => n.id),
		p.edges.map((e) => e.id),
	)}
	>
		<Background />
		<Controls />
		<MiniMap />
		<Panel position="top-left">
			<WorkflowToolbar />
		</Panel>
		<Panel position="top-right">
			<NodePalette />
		</Panel>
		{#if invalidMsg}
			<Panel position="bottom-center">
				<div
					class="border-destructive/40 bg-destructive/10 text-destructive rounded-md border px-3 py-1.5 text-xs font-medium shadow"
				>
					{invalidMsg}
				</div>
			</Panel>
		{/if}
	</SvelteFlow>
</div>

{#if menu}
	<ContextMenu {menu} onClose={() => (menu = null)} />
{/if}
