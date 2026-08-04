<script lang="ts">
	/** Ray cluster nodes — the SAME `ClusterBoard` the /compute route renders, not a second table over the same
	 *  remote. The board owns its own reads, polling and layout; the panel only gives it a box. */
	import ClusterBoard from '$lib/boards/ClusterBoard.svelte';
</script>

<ClusterBoard />
