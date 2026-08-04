<script lang="ts">
	/**
	 * The run board — the SAME `RunsBoard` the /lineage/runs page renders.
	 *
	 * Rows come from the dock's shared `LineageState`, not a fetch of this panel's own: the graph, the
	 * event feed and this board are ONE poll, which is the property that earns the dock. The board
	 * takes rows as a prop precisely so both callers can keep their own source.
	 */
	import RunsBoard from '$lib/lineage/RunsBoard.svelte';
	import { getBench } from '$lib/dock/bench.svelte';

	const store = getBench();
</script>

<RunsBoard runs={store.runs} />
