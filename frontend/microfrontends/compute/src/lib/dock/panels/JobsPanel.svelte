<script lang="ts">
	/** Submitted Ray jobs — the SAME `JobsBoard` the /compute route renders, not a second table over the same
	 *  remote. The board owns its own reads, polling and layout; the panel only gives it a box. */
	import JobsBoard from '$lib/boards/JobsBoard.svelte';
</script>

<JobsBoard />
