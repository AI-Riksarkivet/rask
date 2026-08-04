<script lang="ts">
	// The annotator shell — three-zone layout (tool rail · resizable canvas+overlays ·
	// review inspector) over ONE media unit. Owns the AnnotatorController + the keyboard
	// controller; every child is dumb + controlled. The /annotator route re-mounts this
	// per unit (via {#key}) so navigating a review selection loads each unit fresh.
	import { viewerFor } from '$lib/viewer/registry';
	import type { MediaUnit } from '$lib/viewer/types';
	import { AnnotatorController } from '$lib/viewer/annotator.svelte';
	import { reviewSelection } from '$lib/labeling/review-selection.svelte';
	import { ontologyTools } from '$lib/projects/types';
	import { fetchTask } from '$lib/projects/remote/tasks.remote';
	import { ResizableSplit } from '@rask/ui/resizable-split';
	import { Badge } from '@rask/ui/badge';
	import AnnotatorToolbar from './AnnotatorToolbar.svelte';
	import AnnotationSidebar from './AnnotationSidebar.svelte';
	import ZoomControls from './ZoomControls.svelte';
	import PageNav from './PageNav.svelte';
	import AiAssistBar from './AiAssistBar.svelte';
	import { TOOL_KEYS, isCvTool } from '../tool-defs';

	let { unit, onexit }: { unit: MediaUnit; onexit?: () => void } = $props();

	const Viewer = $derived(viewerFor(unit.kind));
	const controller = new AnnotatorController();

	// Read the task's captured template so the RAIL can decline to offer a tool the task will
	// refuse. Enforcement stays server-side at submit — this only moves the same contract earlier,
	// so a violation is a tool that is not there rather than a 409 after the work. Only when opened
	// FROM a task: an ad-hoc `?keys=` canvas has no contract and stays unconstrained.
	$effect(() => {
		const id = reviewSelection.taskId;
		if (!id) {
			controller.allowedShapeTypes = [];
			controller.relationNames = [];
			return;
		}
		let alive = true;
		void fetchTask({ taskId: id })
			.then((result) => {
				if (!alive) return;
				// DERIVED from the taxonomy, never read from a list beside it: `ontologyTools` is the
				// union of every class's tools and is empty when any class is unconstrained. That
				// replaces `enforce ? tools : []`, where a flat `tools` list could contradict the very
				// classes it sat next to — and where one false flag voided every declaration a
				// manager had explicitly written.
				controller.allowedShapeTypes = result.ok ? ontologyTools(result.data.ontology) : [];
				// The relations this task DECLARES. Empty means the inspector offers no link rail at
				// all — a control that can produce nothing reads as broken rather than inapplicable.
				controller.relationNames = result.ok
					? (result.data.ontology?.relations ?? []).map((r) => r.name)
					: [];
			})
			.catch(() => {
				// A failed read must not silently narrow the rail: unconstrained is the honest
				// fallback, and the 409 still backstops it.
				if (alive) {
					controller.allowedShapeTypes = [];
					controller.relationNames = [];
				}
			});
		return () => {
			alive = false;
		};
	});
	// AUTOSAVE. Started only for a canvas opened FROM A TASK: that is the surface where losing work
	// costs someone their afternoon, and it is the one with a draft to reconcile against. The ad-hoc
	// `?keys=` canvas keeps the explicit-save behaviour it has always had.
	//
	// The debounce is driven from HERE rather than inside the controller because a component has a
	// lifecycle and a class does not — this is what guarantees the timer dies with the canvas
	// instead of firing a write into a unit nobody is looking at any more.
	$effect(() => {
		if (reviewSelection.taskId) controller.startAutosave();
		else controller.stopAutosave();
		return () => controller.stopAutosave();
	});
	$effect(() => {
		// Reading `dirty` is the subscription: every edit re-runs this and pushes the timer out, so a
		// burst of typing produces ONE write rather than one per keystroke.
		void controller.dirty;
		controller.scheduleAutosave();
	});

	let status = $state('loading…');
	// True when the unit's media/annotations failed to load — the status chip turns
	// destructive and carries the reason (never a silent, eternal "loading…").
	let loadFailed = $state(false);

	// Audio is temporal-only (no canvas): hide the spatial chrome — drawing tools, zoom,
	// and the GroundingDINO box-assist. Image + video keep it (video draws on its frame).
	const spatial = $derived(unit.kind !== 'audio');

	// Page nav = the review selection (else this single unit). Navigating drives the
	// shared store, whose index change re-mounts this shell with the next unit.
	const pages = $derived(
		reviewSelection.total > 0
			? reviewSelection.units.map((u, i) => ({ key: u.key, label: `#${i + 1}` }))
			: [{ key: unit.key, label: 'p' }],
	);
	const pageIndex = $derived(reviewSelection.total > 0 ? reviewSelection.index : 0);
	function navigate(i: number): void {
		if (reviewSelection.total > 0) reviewSelection.go(i);
	}

	function onKeydown(e: KeyboardEvent): void {
		const el = e.target as HTMLElement | null;
		if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
		const k = e.key.toLowerCase();
		if (e.ctrlKey || e.metaKey) {
			if (k === 'z') {
				e.preventDefault();
				if (e.shiftKey) controller.redo();
				else controller.undo();
			} else if (k === 'y') {
				e.preventDefault();
				controller.redo();
			} else if (k === 's') {
				e.preventDefault();
				void controller.save();
			}
			return;
		}
		const tool = TOOL_KEYS[k];
		if (tool) {
			// Drawing hotkeys need a live spatial engine (controller.ctx) — on a temporal
			// (audio) unit they'd arm a phantom tool with no canvas behind it, and the
			// forwarding below would then swallow the review hotkeys into a no-op.
			const cvTool = isCvTool(tool);
			const spatialTool = tool !== 'select' && tool !== 'pan';
			if (
				(controller.canDraw || !spatialTool) &&
				(!spatialTool || controller.ctx !== null) &&
				(!cvTool || controller.cvCapable)
			) {
				controller.setTool(tool);
			}
			return;
		}
		// While a DRAWING tool is active on a spatial canvas, Enter/Escape/Backspace belong
		// to the tool (polygon/brush/magnetic commit · cancel-in-progress · vertex undo) —
		// not to the review hotkeys, which would otherwise swallow them. Skip when a button/
		// select has focus (Enter must still activate it) — the top guard already skips inputs.
		const drawingActive =
			controller.ctx !== null &&
			controller.canDraw &&
			controller.activeTool !== 'select' &&
			controller.activeTool !== 'pan';
		const focusable = el && (el.tagName === 'BUTTON' || el.tagName === 'SELECT');
		if (
			drawingActive &&
			!focusable &&
			(e.key === 'Enter' || e.key === 'Escape' || e.key === 'Backspace')
		) {
			e.preventDefault();
			controller.forwardToolKey(e.key);
			return;
		}
		if (k === 'a' || e.key === 'Enter') {
			controller.acceptAndAdvance('accepted');
		} else if (k === 'r') {
			controller.acceptAndAdvance('rejected');
		} else if (k === 'j' || e.key === 'ArrowDown') {
			e.preventDefault();
			controller.next();
		} else if (k === 'k' || e.key === 'ArrowUp') {
			e.preventDefault();
			controller.prev();
		} else if (e.key === 'Delete' || e.key === 'Backspace') {
			controller.deleteSelected();
		} else if (k === 'p') {
			controller.convertToPolygon();
		} else if (e.key === 'Escape') {
			controller.select(null);
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<!-- h-full/w-full (not h-screen): the shell now sits under the estate navbar in the zone layout. -->
<div class="flex h-full w-full">
	<AnnotatorToolbar {controller} {spatial} {onexit} />

	<div class="min-w-0 flex-1">
		<ResizableSplit storageKey="lance-media-annotate" initial={0.72} minLeft={420} minRight={320}>
			{#snippet left()}
				<div class="relative h-full w-full">
					<!-- The load/status chip is a real Badge — secondary while healthy, destructive when
					     the unit failed to load — instead of a hand-rolled black pill that ignored the
					     theme entirely (it stayed dark-on-white in light mode). It also moves off the
					     TOP-left, where the centred assist bar painted over the tail of any message
					     longer than a few words (a load failure, always, exactly when you need to read
					     it); bottom-left is the one free corner — page nav is bottom-centre, zoom is
					     bottom-right — so it can render its full text. -->
					<Badge
						variant={loadFailed ? 'destructive' : 'secondary'}
						class="absolute bottom-2 left-2 z-10 font-mono shadow-sm backdrop-blur"
						data-testid="annotate-status"
					>
						annotate · {unit.kind} · {status}{controller.saveStatus ? ` · ${controller.saveStatus}` : ''}
					</Badge>
					<Viewer
						{unit}
						{controller}
						onload={(n) => {
	status = `${n} annotations from Lance`;
	loadFailed = false;
}}
						onerror={(message) => {
	status = `load failed — ${message}`;
	loadFailed = true;
}}
					/>
					{#if spatial && controller.canDraw}
						<AiAssistBar {controller} taskId={reviewSelection.taskId} />
					{/if}
					<PageNav {pages} current={pageIndex} onNavigate={navigate} />
					{#if spatial}
						<ZoomControls {controller} />
					{/if}
				</div>
			{/snippet}
			{#snippet right()}
				<AnnotationSidebar {controller} />
			{/snippet}
		</ResizableSplit>
	</div>
</div>
