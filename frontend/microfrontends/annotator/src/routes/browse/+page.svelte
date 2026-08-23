<script lang="ts">
	// The corpus browser (datasets → documents → chunks) — the zone's OLD landing, relocated when
	// the projects landing took `/` (S9). Two things happen here now, and they are different jobs:
	//
	//   OPEN   — one person reads these chunks on the canvas, right now.
	//   SELECT — these chunks are queued for a TEAM to label, as tasks in a project.
	//
	// The second is step 1 of the bulk-labeling design (`open_browse.md`). Browse could always find
	// keys and could only ever open them; sending them into a project meant already standing inside
	// that project. So the workflow the product exists for — "find these five hundred pages, label
	// them" — had no path through it.
	//
	// Nothing new server-side: the bar calls the existing `sendItems`. Bulk labeling is deliberately
	// NOT a new write path.
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import BulkSendBar from '$lib/select/BulkSendBar.svelte';
	import DataSelection from '$lib/select/DataSelection.svelte';

	/** The keys queued for a bulk send, and the corpus they came from. Held HERE rather than in the
	 *  bar so the selection survives navigating back to the document list — you pick chunks from
	 *  several documents, then send once. */
	let selected = $state<string[]>([]);
	let selectedDataset = $state<string | null>(null);
	let selectedVersion = $state<number | null>(null);

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

	function select(keys: string[], dataset: string | null, datasetVersion: number | null): void {
		// Switching corpus REPLACES the selection rather than merging. Keys are namespaced per corpus,
		// so a mixed list would send items resolving against the wrong one — the stale-item defect #37
		// refuses that at the write boundary, but only after someone has waited for it.
		const merged = dataset === selectedDataset ? [...selected, ...keys] : keys;
		selectedDataset = dataset;
		selectedVersion = datasetVersion;
		selected = [...new Set(merged)];
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="min-h-0 flex-1 overflow-hidden">
		<DataSelection
			onopen={open}
			onselect={select}
			selectedKeys={selected}
			initialDataset={page.url.searchParams.get('dataset')}
		/>
	</div>
	<BulkSendBar
		keys={selected}
		dataset={selectedDataset}
		datasetVersion={selectedVersion}
		onsent={() => {
			// Cleared on success: leaving them selected invites a second send of the same items, and
			// two tasks for one page is two people labelling it.
			selected = [];
		}}
	/>
</div>
