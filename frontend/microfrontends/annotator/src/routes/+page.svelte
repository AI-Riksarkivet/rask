<script lang="ts">
	// Thin annotator route. `?keys=doc/speech/chunk,…` (the read plane's review-selection
	// bridge — atlas lasso / search / a claimed task's Annotate button) opens the annotate
	// canvas, re-mounted per active key so navigating the selection loads each unit fresh.
	// With no keys the PROJECTS LANDING renders instead (S9: "landing = your projects and
	// their progress" — the DataSelection gallery moved to /browse). The URL is the source
	// of truth, so a canvas reload restores the same unit.
	import { browser } from '$app/environment';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import type { MediaKind } from '$lib/viewer/types';
	import { reviewSelection } from '$lib/labeling/review-selection.svelte';
	import ProjectsLanding from '$lib/projects/ProjectsLanding.svelte';
	import AnnotatorShell from '$lib/viewer/layout/AnnotatorShell.svelte';
	import CompareView from '$lib/viewer/layout/CompareView.svelte';
	import { exitHref } from '$lib/viewer/exit-target';
	import { fetchMeViaBff } from '$lib/http';
	import { listTasks } from '$lib/projects/remote/projects.remote';
	import { claimedStream, streamPosition, type StreamTask } from '$lib/viewer/task-stream';

	// `authEnabled` rides the LAYOUT data, which resolves it server-side. The canvas must not
	// re-derive it from whether a session exists: a 404 on `capi/v1/me` means BOTH "auth is off" and
	// "the identity lookup failed", and collapsing those two is what made the first cut of the label
	// stream permanently empty on an ungoverned stack.
	let { data }: { data: { authEnabled: boolean } } = $props();

	function openFromParams(params: URLSearchParams): void {
		const keys = params.get('keys');
		if (!keys) {
			reviewSelection.clear();
			return;
		}
		// Beyond `keys`, the deep-link takes a modality override (`kind=audio|video` → the
		// temporal viewers), an optional same-origin `media=` source (a specific clip) and
		// the picked dataset (`dataset=` — absent for the backend default), so a reload
		// reopens the same unit IN the same dataset (frame/annotations/save alike).
		const rawKind = params.get('kind');
		const kind: MediaKind =
			rawKind === 'audio' || rawKind === 'video' || rawKind === 'text' ? rawKind : 'image';
		const rawMedia = params.get('media');
		const media = rawMedia?.startsWith('/') ? rawMedia : undefined; // same-origin only
		reviewSelection.openKeys(
			keys.split(','),
			kind,
			media,
			params.get('dataset'),
			params.get('task'),
		);
	}

	// Init synchronously (before first render) so a deep link never flashes the landing.
	if (browser) openFromParams(new URLSearchParams(window.location.search));

	// Track later URL changes (selection-view goto, back/forward) — guarded against
	// re-opening the keys the store already holds. The param is normalized exactly like
	// openKeys (empty segments dropped), otherwise a hand-edited link such as
	// `?keys=doc/0/1,` would never equal the held keys and the effect would loop.
	$effect(() => {
		const params = page.url.searchParams;
		const wanted = (params.get('keys') ?? '').split(',').filter(Boolean).join(',');
		const held = reviewSelection.units.map((u) => u.key).join(',');
		const datasetDrifted =
			wanted !== '' && (params.get('dataset') ?? '') !== (reviewSelection.dataset ?? '');
		if (wanted !== held || datasetDrifted) openFromParams(params);
	});

	const unit = $derived(reviewSelection.active);

	// Exit returns to WHERE YOU CAME FROM, which the URL has been carrying all along.
	//
	// This used to always go to `?dataset=…` — the corpus browser — with a comment claiming a
	// task-opened canvas "exits back to wherever the annotator came from". It did not: finish an
	// item from your labeling queue, press exit, and you land in a document gallery with the queue
	// you were working through nowhere in sight. Reported from the running app.
	function exit(): void {
		void goto(exitHref(page.url.searchParams, base), { keepFocus: true, noScroll: true });
	}

	// THE LABEL STREAM — the items the canvas can walk while you work.
	//
	// Loaded HERE rather than in the shell because the shell is re-mounted per key (`{#key unit.key}`
	// below) and the stream is a property of the QUEUE, not of the item on screen. Fetching it inside
	// would re-read the whole task list on every page turn within a document.
	//
	// It is keyed on the PROJECT, not on the task: hopping to the next item changes `task=` but stays
	// in the same queue, so the list is already correct and must not be re-fetched mid-hop.
	//
	// The svelte MCP autofixer flags the assignments below ("stateful variable assigned inside an
	// $effect"). DECLINED, with reasons, rather than silently ignored:
	//   * It cannot be `$derived` — this is an async read, and `$derived` is for synchronous
	//     computation over already-held state.
	//   * It cannot be `onMount` (the `AI assistance` page's fix) — the project can change without
	//     this component remounting, and a stream that went stale on navigation is the bug.
	//   * It should not be `load` in `+page.ts` — this route is `ssr = false` BECAUSE it is the Pixi
	//     canvas, so a blocking load would delay the drawing surface on a queue read that only
	//     decorates it. The canvas must paint first; the stream control appears when it resolves.
	// The guard (`project === loadedProject`) is what keeps it from looping, which is the real hazard
	// the warning is pointing at.
	let me = $state<string | null>(null);
	let queue = $state<StreamTask[]>([]);
	let loadedProject = $state<string | null>(null);

	const projectId = $derived(page.url.searchParams.get('project'));
	const taskId = $derived(page.url.searchParams.get('task'));

	$effect(() => {
		const project = projectId;
		// No project → this canvas came from the corpus browser and there is no queue behind it.
		if (!project || project === loadedProject) return;
		loadedProject = project;
		void (async () => {
			// The identity resolves once per canvas; the queue follows the project. Both are read
			// before either is used, so a half-loaded stream never renders a wrong position.
			const [subject, tasks] = await Promise.all([
				me === null ? fetchMeViaBff().then((r) => r?.sub ?? null) : Promise.resolve(me),
				// `details: true` — the bare listing is counts plus an id→state map, which cannot say
				// WHO holds a task or which keys it points at, and the stream needs both.
				listTasks({ projectId: project, details: true }),
			]);
			me = subject;
			queue = tasks.ok ? (tasks.data.details ?? []) : [];
		})();
	});

	const stream = $derived(
		streamPosition(claimedStream(queue, me, data.authEnabled), taskId, projectId, base),
	);

	// `?view=compare` swaps the one-at-a-time canvas for the side-by-side COMPARE view — the
	// pairwise/preference question over the item's candidates. The two never mount together: the
	// compare view writes each pane directly over the plain transport, and a concurrently mounted
	// canvas controller would be a second writer racing its autosave on the same tables.
	const comparing = $derived(
		page.url.searchParams.get('view') === 'compare' && reviewSelection.total > 1,
	);
	const canvasHref = $derived.by(() => {
		const u = new URL(page.url);
		u.searchParams.delete('view');
		return u.pathname + u.search;
	});
</script>

{#if unit && comparing}
	<CompareView units={reviewSelection.units} taskId={reviewSelection.taskId} {canvasHref} />
{:else if unit}
	{#key unit.key}
		<AnnotatorShell {unit} onexit={exit} {stream} />
	{/key}
{:else}
	<ProjectsLanding />
{/if}
