<script lang="ts">
	/**
	 * The S3 object browser — the component `/lakehouse/catalog/storage` renders, unchanged.
	 *
	 * Like `TablesPanel` this is a sizing wrapper over the zone's real component — no second copy.
	 */
	import ObjectBrowser from '$lib/storage/ObjectBrowser.svelte';
</script>

<div class="bg-background h-full min-h-0 overflow-auto">
	<ObjectBrowser />
</div>
