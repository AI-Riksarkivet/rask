<script lang="ts">
	/**
	 * The lineage DAG as a dock panel — the same `LineageGraph` the full page renders.
	 *
	 * This panel is the live-state drag test's sharpest case: Svelte Flow holds its pan/zoom viewport,
	 * its node selection and every node position the user dragged in `$state.raw` INSIDE the component.
	 * If the dock remounted a panel on a layout change, all three would reset on the first drag — which
	 * is exactly the failure `SveltePanelRenderer`'s mount-once invariant exists to prevent.
	 */
	import LineageGraph from '$lib/lineage/LineageGraph.svelte';
	import { getLineageState } from '$lib/dock/lineage-context';

	const store = getLineageState();
</script>

<div class="wrap">
	<LineageGraph {store} />
</div>

<style>
	.wrap {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
	}
</style>
