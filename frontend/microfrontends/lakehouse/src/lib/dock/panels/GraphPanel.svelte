<script lang="ts">
	/** The medallion DAG — the zone's real `LineageGraph`, over the dock's shared store. */
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import LineageGraph from '$lib/lineage/LineageGraph.svelte';
	import { getBench } from '$lib/dock/bench.svelte';

	const store = getBench();
</script>

<!--
	FLEX COLUMN, not a plain block. `LineageGraph`'s root is `flex: 1 1 0` — correct for the /lineage
	page, which hands it a flex parent — so inside a block wrapper it resolves against nothing and
	collapses to 0px. Measured here before the fix: the panel 277px, the section inside it 0px, the
	flow pane 0px; `fitView` then framed six real nodes against an empty box and pinned the viewport
	at scale(0.1). The graph was present, correct, and invisible.

	Same defect as the dock's own zero height (#83), one level further in: a `flex: 1 1 0` child is
	only ever sized by a FLEX parent, and a panel's content wrapper is the last place anyone checks.
-->
<div class="flex h-full min-h-0 flex-col">
	<LineageGraph {store} {base} navigate={(href) => void goto(href)} />
</div>
