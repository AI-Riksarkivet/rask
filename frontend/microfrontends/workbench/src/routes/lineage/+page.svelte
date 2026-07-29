<script lang="ts">
	/** The lineage graph on its own, full-bleed — the same component the dock mounts as a panel, for
	 *  when you want the DAG and nothing else. One implementation, two framings. */
	import { LineageGraph, LineageState } from '@rask/panels/lineage';
	import { createLineageClient } from '@rask/api/lineage';
	import { createBffClient } from '@rask/api/client';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';

	const store = new LineageState(createLineageClient(createBffClient(base)));
	onMount(() => void store.poll());
</script>

<svelte:head><title>Lineage graph · Workbench</title></svelte:head>

<div class="graph">
	<LineageGraph {store} base={`${base}`} navigate={(href) => void goto(href)} />
</div>

<style>
	.graph {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		width: 100%;
	}
</style>
