<script lang="ts">
	// The corpus browser (datasets → documents → chunks) — the zone's OLD landing, relocated when
	// the projects landing took `/` (S9). Still the place to find keys to send into a project, and
	// still opens the canvas directly for ad-hoc viewing.
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import DataSelection from '$lib/select/DataSelection.svelte';

	// A non-default dataset rides the deep link (`?dataset=…&keys=…`) so the canvas — and a reload
	// of its URL — targets the picked dataset; the default keeps the bare `?keys=` link byte-identical.
	// Absolute via `base`: a relative `../` from an unslashed /browse resolves past the zone base.
	function open(keys: string[], dataset: string | null): void {
		const ds = dataset ? `dataset=${encodeURIComponent(dataset)}&` : '';
		void goto(`${base}/?${ds}keys=${encodeURIComponent(keys.join(','))}`, {
			keepFocus: true,
			noScroll: true,
		});
	}
</script>

<DataSelection onopen={open} initialDataset={page.url.searchParams.get('dataset')} />
