<script lang="ts">
	// The annotator shell — three-zone layout (tool rail · resizable canvas+overlays ·
	// review inspector) over ONE media unit. Owns the AnnotatorController + the keyboard
	// controller; every child is dumb + controlled. The /annotator route re-mounts this
	// per unit (via {#key}) so navigating a review selection loads each unit fresh.
	import { page } from '$app/state';
	import { viewerFor } from '$lib/viewer/registry';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';
	import { onRemoteChange } from '../remote-change';
	import type { MediaUnit } from '$lib/viewer/types';
	import { AnnotatorController } from '$lib/viewer/annotator.svelte';
	import { reviewSelection } from '$lib/labeling/review-selection.svelte';
	import { ontologyTools } from '$lib/projects/types';
	import { fetchDraft, fetchTask } from '$lib/projects/remote/tasks.remote';
	import { ResizableSplit } from '@rask/ui/resizable-split';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import AnnotatorToolbar from './AnnotatorToolbar.svelte';
	import AnnotationSidebar from './AnnotationSidebar.svelte';
	import ClassificationBar from './ClassificationBar.svelte';
	import DocumentTextLane from './DocumentTextLane.svelte';
	import ZoomControls from './ZoomControls.svelte';
	import PageNav from './PageNav.svelte';
	import TaskStreamNav from './TaskStreamNav.svelte';
	import Filmstrip from './Filmstrip.svelte';
	import type { StreamPosition } from '../task-stream';
	import AiAssistBar from './AiAssistBar.svelte';
	import { TOOL_KEYS, isCvTool } from '../tool-defs';

	// `stream` is the LABEL STREAM position, computed by the route (which owns the queue read — the
	// shell is re-mounted per key and would otherwise re-fetch the task list on every page turn).
	// Optional so a canvas opened outside a queue, and every existing test that mounts this shell,
	// keeps working with no stream control at all.
	let {
		unit,
		onexit,
		stream = {
			position: 0,
			total: 0,
			prevHref: null,
			nextHref: null,
			active: false,
			items: [],
			taskId: null,
			projectId: null,
		},
	}: { unit: MediaUnit; onexit?: () => void; stream?: StreamPosition } = $props();

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
			controller.textSpanClasses = [];
			controller.tagClasses = [];
			controller.classAttributes = {};
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
				// The classes a TEXT SPAN may be labelled with, from the ontology rather than from
				// whatever labels happen to be on the page already.
				controller.textSpanClasses = result.ok
					? (result.data.ontology?.classes ?? [])
							.filter((c) => c.tools.includes('text'))
							.map((c) => c.name)
					: [];
				// The classes the ITEM may be tagged with — the classification question's choices.
				controller.tagClasses = result.ok
					? (result.data.ontology?.classes ?? [])
							.filter((c) => c.tools.includes('tag'))
							.map((c) => c.name)
					: [];
				// The typed per-class ATTRIBUTES (reading order, script, …) — the inspector's
				// attribute editor renders from these, per selected row's class.
				controller.classAttributes = result.ok
					? Object.fromEntries(
							(result.data.ontology?.classes ?? [])
								.filter((c) => (c.attributes ?? []).length > 0)
								.map((c) => [
									c.name,
									(c.attributes ?? []).map((a) => ({
										name: a.name,
										type: (a.type ?? 'free') as 'free' | 'int' | 'enum' | 'bool',
										choices: a.choices ?? [],
										required: a.required ?? false,
									})),
								]),
						)
					: {};
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
	// RESTORE the draft's relations. Links live on the task DRAFT, not the annotations table, and
	// nothing read them back — reopening a task showed zero links, so the next draft sync posted an
	// empty list and silently destroyed every drawn relation. One keyed read at open closes the
	// loop; `restoreLinks` declines to clobber links the person drew before this resolved.
	$effect(() => {
		const id = reviewSelection.taskId;
		if (!id) return;
		let alive = true;
		const draftQuery = fetchDraft({ taskId: id });
		void draftQuery
			.refresh()
			.then(() => {
				if (!alive) return;
				const current = draftQuery.current;
				if (current?.ok && current.data.links.length > 0)
					controller.restoreLinks(current.data.links);
			})
			.catch(() => {
				// No draft (a fresh task) or an unreachable read — nothing to restore, and the draft
				// sync's own revision guard still protects the next write.
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

	/** #73 — the estate's FIRST cursor-driven Arrow read.
	 *
	 *  The annotations table was read once, at mount. Two annotators on the same page never saw each
	 *  other's work, and neither did one person with the item open in two tabs — the second save then
	 *  went out against a `base_version` from before the first, which is exactly the conflict OCC
	 *  refuses, so their work was rejected AFTER they did it.
	 *
	 *  The CURSOR is a value and rides `query.live`; the BYTES stay on their own route and are simply
	 *  re-fetched. That is the transport rule followed, not bent: Arrow never travels through a
	 *  remote function, only the number that says it changed.
	 *
	 *  Remounting the viewer is the reload — `onready` re-runs `loadAnnotations` and re-attaches with
	 *  the new version — so there is no second load path to keep in agreement with the first. */
	let reloadNonce = $state(0);
	let remoteNotice = $state<string | null>(null);

	// `liveRead`'s FIRST read is unconditional by design (a surface must render without waiting for a
	// stream). Here that first call would bump the nonce, remount the viewer, re-register liveRead,
	// and fire again — an infinite remount. `primed` makes the first call mean "I have arrived",
	// which is what it actually is; only a LATER advance is a change someone else made.
	let primed = false;
	liveRead(lineageTick, () => {
		if (!primed) {
			primed = true;
			return;
		}
		const decision = onRemoteChange({
			dirty: controller.dirty,
			saving: controller.saving,
			attached: controller.attached,
		});
		if (decision.action === 'reload') reloadNonce += 1;
		else if (decision.action === 'warn') remoteNotice = decision.reason;
	});

	let status = $state('loading…');
	// True when the unit's media/annotations failed to load — the status chip turns
	// destructive and carries the reason (never a silent, eternal "loading…").
	let loadFailed = $state(false);

	// Audio is temporal-only and TEXT is the document-as-canvas — neither has a pixel surface, so
	// both hide the spatial chrome (drawing tools, zoom, the box-assist). Image + video keep it
	// (video draws on its frame).
	const spatial = $derived(unit.kind !== 'audio' && unit.kind !== 'text');
	// Temporal units render a TRANSPORT BAR at the bottom of the viewer, inside the same
	// positioned ancestor the floating pills anchor to — so `bottom-2` overlays the bar and eats
	// its clicks (found by Playwright's interception check on the video seek slider). Raised, not
	// force-clicked around: the overlap is the app's defect, not the test's.
	const raisedOverTransport = $derived(
		// Video stacks TWO strips under the frame (the audio lane + the transport); audio only the
		// transport. The pills clear whatever their unit actually renders beneath them.
		unit.kind === 'video' ? 'bottom-32' : unit.kind === 'audio' ? 'bottom-14' : undefined,
	);

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

	// A multi-key item can also be judged BETWEEN its units — the compare view (the route swaps
	// this shell for it on `?view=compare`). Offered from the page bar because that is the control
	// that already knows the item has more than one unit.
	const compareHref = $derived.by(() => {
		if (reviewSelection.total < 2) return null;
		const u = new URL(page.url);
		u.searchParams.set('view', 'compare');
		return u.pathname + u.search;
	});

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

<!-- COLUMN, not row (#68). The toolbar is a top bar now, so the three vertical things below it —
     filmstrip, canvas, annotation sidebar — get the full height beneath it. Reported: "gallery of
     itmes / bandroll on the left side in labeling canvas view and the sidebar toolbar horizonally
     over the canvas".
     h-full/w-full (not h-screen): the shell sits under the estate navbar in the zone layout. -->
<div class="flex h-full w-full flex-col">
	<AnnotatorToolbar {controller} {spatial} {onexit}>
		{#snippet assist()}
			{#if spatial && controller.canDraw}
				<AiAssistBar {controller} taskId={reviewSelection.taskId} />
			{/if}
		{/snippet}
	</AnnotatorToolbar>

	<div class="flex min-h-0 flex-1">
		<!-- The items live on the LEFT edge, which is why the toolbar had to leave it. -->
		<Filmstrip
			items={stream.items}
			activeTaskId={stream.taskId}
			projectId={stream.projectId ?? ''}
		/>

		<div class="min-w-0 flex-1">
			<ResizableSplit storageKey="lance-media-annotate" initial={0.72} minLeft={420} minRight={320}>
				{#snippet left()}
					<!-- The central WORKSPACE is a column: the media viewer (with its floating overlays)
					     on top, the transcription lane under it — the Label-Studio composition, where
					     the document's text is part of the labeling canvas rather than a side panel.
					     The overlays' positioned ancestor is the VIEWER box, so the pills never stray
					     onto the lane. -->
					<div class="flex h-full w-full flex-col">
						<ClassificationBar {controller} />
						<div class="relative min-h-0 w-full flex-1">
							<!-- The load/status chip is a real Badge — secondary while healthy, destructive when
					     the unit failed to load — instead of a hand-rolled black pill that ignored the
					     theme entirely (it stayed dark-on-white in light mode). It also moves off the
					     TOP-left, where the centred assist bar painted over the tail of any message
					     longer than a few words (a load failure, always, exactly when you need to read
					     it); bottom-left is the one free corner — page nav is bottom-centre, zoom is
					     bottom-right — so it can render its full text. -->
							<Badge
								variant={loadFailed ? 'destructive' : 'secondary'}
								class={cn(
	'pointer-events-none absolute bottom-2 left-2 z-10 font-mono shadow-sm backdrop-blur',
	raisedOverTransport,
)}
								data-testid="annotate-status"
							>
								annotate · {unit.kind} · {status}{controller.saveStatus
									? ` · ${controller.saveStatus}`
									: ''}
							</Badge>
							{#key reloadNonce}
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
							{/key}
							{#if remoteNotice}
								<!-- A change someone else made, on a canvas that has unsaved work. NEVER reloaded
						     over — the shapes on screen are theirs and cannot be got back. It names the
						     consequence (the save will be refused) because "changed" alone leaves a person
						     unable to decide. -->
								<div
									class="border-destructive/50 bg-card/95 text-card-foreground pointer-events-auto absolute top-2 left-1/2 z-20 flex max-w-md -translate-x-1/2 items-start gap-2 rounded-lg border p-2 shadow-sm backdrop-blur"
									data-testid="remote-change-notice"
								>
									<p class="text-xs">{remoteNotice}</p>
									<Button
										variant="outline"
										size="sm"
										class="h-6 shrink-0 px-2 text-xs"
										onclick={() => {
	remoteNotice = null;
	reloadNonce += 1;
}}
									>
										Reload
									</Button>
								</div>
							{/if}
							<TaskStreamNav {stream} />
							<PageNav
								{pages}
								current={pageIndex}
								onNavigate={navigate}
								{compareHref}
								class={raisedOverTransport}
							/>
							{#if spatial}
								<ZoomControls {controller} class={raisedOverTransport} />
							{/if}
						</div>
						{#if unit.kind !== 'text'}
							<!-- The OCR composition: image/video on top, its transcription under it. A TEXT
							     unit's viewer IS the document, so a lane would render the same text twice. -->
							<DocumentTextLane {controller} />
						{/if}
					</div>
				{/snippet}
				{#snippet right()}
					<AnnotationSidebar {controller} />
				{/snippet}
			</ResizableSplit>
		</div>
	</div>
</div>
