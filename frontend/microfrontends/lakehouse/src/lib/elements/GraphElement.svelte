<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-graph>` — the lineage DAG, served by the lakehouse zone to the global
	 * workbench. The heavy piece: it mounts the SAME LineageGraph the zone's own /lineage page
	 * renders, over the elements' singleton store (one poller for graph+runs+events on the page).
	 * The xyflow + @rask/flow stylesheets are injected by the elements entry under `@layer base`
	 * (see index.ts) — the CSS answer the plan said this element had to settle.
	 *
	 * `navigate` is a HARD navigation on purpose: a node click leads to a lakehouse route, and a
	 * cross-zone hop must be a full document load (the estate's cross-zone rule).
	 */
	import LineageGraph from '../lineage/LineageGraph.svelte';
	import { ensurePolling, lineage } from './store';

	$effect(() => {
		ensurePolling();
	});
</script>

<div class="graph-host">
	<LineageGraph
		store={lineage}
		base="/lakehouse"
		navigate={(href) => {
	window.location.href = href;
}}
	/>
</div>

<style>
	.graph-host {
		display: block;
		position: relative;
		height: 100%;
		width: 100%;
	}
</style>
