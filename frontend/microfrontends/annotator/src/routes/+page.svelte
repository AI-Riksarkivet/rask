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
	import { exitHref } from '$lib/viewer/exit-target';

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
		const kind: MediaKind = rawKind === 'audio' || rawKind === 'video' ? rawKind : 'image';
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
</script>

{#if unit}
	{#key unit.key}
		<AnnotatorShell {unit} onexit={exit} />
	{/key}
{:else}
	<ProjectsLanding />
{/if}
