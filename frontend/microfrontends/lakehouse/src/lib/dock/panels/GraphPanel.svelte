<script lang="ts">
	/** The medallion DAG — the zone's real `LineageGraph`, over the dock's shared store. */
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import LineageGraph from '$lib/lineage/LineageGraph.svelte';
	import { getBench } from '$lib/dock/bench.svelte';

	const store = getBench();
</script>

<div class="h-full min-h-0">
	<LineageGraph {store} {base} navigate={(href) => void goto(href)} />
</div>
