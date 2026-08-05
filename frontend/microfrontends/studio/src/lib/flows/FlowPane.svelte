<script lang="ts">
	/** The Svelte Flow pane + editor interactions. Lives INSIDE a
	 *  <SvelteFlowProvider> (see FlowsCanvas) so `useSvelteFlow()` works for the
	 *  palette drop. Wires: arrowheads, snap-grid, connection validation with an
	 *  invalid-connection toast, delete confirmation + cleanup, undo/redo keys,
	 *  and the debounced autosave/checkpoint effect.
	 *
	 *  The node library lives in the ZONE RAIL, not on the canvas, so it reaches this
	 *  component two ways and neither couples the rail to the store: a DataTransfer
	 *  payload on drop, or `ADD_NODE_EVENT` when a row is clicked — positioning is
	 *  ours either way, because only the canvas knows where its viewport is. */
	import {
		Background,
		Controls,
		MarkerType,
		MiniMap,
		Panel,
		SvelteFlow,
		useSvelteFlow,
		type Connection,
		type Edge,
	} from '@xyflow/svelte';
	import { on } from 'svelte/events';
	import { useColorMode } from '@rask/ui/color-mode';
	import { graph, isNodeKind } from '$lib/flows/graph.svelte';
	import { nodeTypes } from '$lib/flows/node-types';
	import FlowsToolbar from '$lib/flows/FlowsToolbar.svelte';
	import { ADD_NODE_EVENT, decodeDrop, NODE_DND_MIME, type PaletteDrop } from '$lib/flows/palette';

	const { screenToFlowPosition } = useSvelteFlow();

	/** The pane element — its centre is where a CLICKED library row lands. */
	let paneEl = $state<HTMLDivElement | null>(null);
	/** Cascade successive click-adds so they don't stack on one spot. */
	let clickAdds = 0;
	const CLICK_ADD_OFFSET_PX = 28;

	/** Debounce before autosave + history checkpoint (one step per settled burst). */
	const AUTOSAVE_DEBOUNCE_MS = 400;
	/** How long an invalid-connection toast stays up. */
	const INVALID_TOAST_MS = 2600;
	/** Canvas snap-to-grid size (px). */
	const SNAP_GRID_PX = 16;

	/** Default arrowhead for every edge (neutral — readable on both themes). */
	const ARROW_MARKER = { type: MarkerType.ArrowClosed, width: 18, height: 18, color: '#94a3b8' };

	// Match the app's theme — reactive, so the flow's colours follow the runtime
	// theme toggle instead of going stale.
	const theme = useColorMode();

	// ── Autosave (snapshot reads deeply → effect re-runs on any change) ────────
	$effect(() => {
		const json = graph.snapshot();
		const timer = setTimeout(() => {
			graph.persist(json);
			graph.checkpoint(json); // settled change → one undo step
		}, AUTOSAVE_DEBOUNCE_MS);
		return () => clearTimeout(timer);
	});

	// ── Connection validation + invalid feedback ───────────────────────────────
	let invalidMsg = $state<string | null>(null);
	let pendingReason: string | null = null;
	let madeConnection = false;
	let msgTimer: ReturnType<typeof setTimeout> | null = null;

	function flash(msg: string): void {
		invalidMsg = msg;
		if (msgTimer) clearTimeout(msgTimer);
		msgTimer = setTimeout(() => (invalidMsg = null), INVALID_TOAST_MS);
	}

	// Don't let a pending toast timer outlive the component.
	$effect(() => () => {
		if (msgTimer) clearTimeout(msgTimer);
	});

	function validate(connection: Connection | Edge): boolean {
		const reason = graph.connectionError(connection);
		if (reason && connection.source && connection.target) pendingReason = reason;
		return reason === null;
	}

	// ── Drag-from-library drop ─────────────────────────────────────────────────
	function onDrop(e: DragEvent): void {
		e.preventDefault();
		// Anything unrecognised (a file dragged onto the pane, say) decodes to null and is ignored.
		const drop = decodeDrop(e.dataTransfer?.getData(NODE_DND_MIME), isNodeKind);
		if (!drop) return;
		graph.addNode(drop.kind, screenToFlowPosition({ x: e.clientX, y: e.clientY }), drop.config);
	}

	// ── Click-to-add from the rail ─────────────────────────────────────────────
	function addAtViewportCentre(drop: PaletteDrop): void {
		if (!paneEl) return;
		const box = paneEl.getBoundingClientRect();
		const step = (clickAdds++ % 5) * CLICK_ADD_OFFSET_PX;
		graph.addNode(
			drop.kind,
			screenToFlowPosition({
				x: box.left + box.width / 2 + step,
				y: box.top + box.height / 2 + step,
			}),
			drop.config,
		);
	}
	function onDragOver(e: DragEvent): void {
		e.preventDefault();
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
	}

	// A window listener rather than `<svelte:window on:…>`: the event name carries a colon, whose
	// only markup form is the LEGACY `on:` directive — mixing that with runes is a compile error.
	// `on` from svelte/events (not raw addEventListener, per oxlint's svelte plugin) returns its own
	// off function, so the effect's teardown is the whole cleanup.
	$effect(() =>
		on(window, ADD_NODE_EVENT, (e) => addAtViewportCentre((e as CustomEvent<PaletteDrop>).detail)),
	);

	// ── Keyboard: undo/redo (ignored while typing) ─────────────────────────────
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
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div bind:this={paneEl} class="h-full w-full" ondragover={onDragOver} ondrop={onDrop}>
	<SvelteFlow
		bind:nodes={graph.nodes}
		bind:edges={graph.edges}
		{nodeTypes}
		colorMode={theme.current}
		fitView
		deleteKey={['Backspace', 'Delete']}
		snapGrid={[SNAP_GRID_PX, SNAP_GRID_PX]}
		defaultEdgeOptions={{ markerEnd: ARROW_MARKER }}
		isValidConnection={validate}
		onnodeclick={({ node }) => graph.inspectNode(node.id)}
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
	// handle — releasing over empty pane is a legitimate cancel.
	if (!madeConnection && pendingReason && connectionState?.toHandle) flash(pendingReason);
}}
		onbeforedelete={async ({ nodes }) => {
	const feedsOthers = nodes.some((n) => graph.dependentsOf(n.id).length > 0);
	return !feedsOthers || window.confirm('Delete node(s) that feed others downstream?');
}}
		ondelete={({ nodes }) => graph.syncDeleted(nodes.map((n) => n.id))}
	>
		<Background />
		<Controls />
		<MiniMap />
		<!-- No palette Panel here: the node library is the ZONE RAIL's (see NodeLibrary.svelte). It
		     was a floating card in this corner first, which gave a searchable, cluster-driven library
		     an 8-rem box over the canvas it was meant to fill. -->
		<Panel position="top-left">
			<FlowsToolbar />
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
