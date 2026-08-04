<script lang="ts">
	/** Ray Serve applications — the SAME `ServeBoard` the /compute route renders, not a second table over the same
	 *  remote. The board owns its own reads, polling and layout; the panel only gives it a box. */
	import ServeBoard from '$lib/boards/ServeBoard.svelte';
</script>

<ServeBoard />
