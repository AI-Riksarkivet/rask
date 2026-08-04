<script lang="ts">
	/** Live Ray actors — the SAME `ActorsBoard` the /compute route renders, not a second table over the same
	 *  remote. The board owns its own reads, polling and layout; the panel only gives it a box. */
	import ActorsBoard from '$lib/boards/ActorsBoard.svelte';
</script>

<ActorsBoard />
