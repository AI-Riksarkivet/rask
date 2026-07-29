<script lang="ts">
	// Thin annotator route. `?keys=doc/speech/chunk,…` (the read plane's review-selection
	// bridge — atlas lasso / search — and now also this zone's own selection view) opens
	// the annotate canvas, re-mounted per active key so navigating the selection loads
	// each unit fresh. With no keys the DATA-SELECTION landing renders instead (datasets →
	// documents → chunks); the old hardcoded demo unit is gone. The URL is the source of
	// truth, so a canvas reload restores the same unit.
	import { browser } from '$app/environment';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import type { MediaKind } from '$lib/viewer/types';
	import { reviewSelection } from '$lib/labeling/review-selection.svelte';
	import DataSelection from '$lib/select/DataSelection.svelte';
	import AnnotatorShell from '$lib/viewer/layout/AnnotatorShell.svelte';

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
		reviewSelection.openKeys(keys.split(','), kind, media, params.get('dataset'));
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

	// A non-default dataset rides the deep link (`?dataset=…&keys=…`) so the canvas —
	// and a reload of its URL — targets the picked dataset; the default keeps the bare
	// `?keys=` link byte-identical. Exit keeps the dataset so the landing re-picks it.
	function open(keys: string[], dataset: string | null): void {
		const ds = dataset ? `dataset=${encodeURIComponent(dataset)}&` : '';
		void goto(`?${ds}keys=${encodeURIComponent(keys.join(','))}`, {
			keepFocus: true,
			noScroll: true,
		});
	}
	function exit(): void {
		const ds = reviewSelection.dataset;
		void goto(ds ? `?dataset=${encodeURIComponent(ds)}` : '?', { keepFocus: true, noScroll: true });
	}
</script>

{#if unit}
	{#key unit.key}
		<AnnotatorShell {unit} onexit={exit} />
	{/key}
{:else}
	<DataSelection onopen={open} initialDataset={page.url.searchParams.get('dataset')} />
{/if}
