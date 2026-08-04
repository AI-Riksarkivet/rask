<script lang="ts">
	/**
	 * The S3 object browser — the component `/lakehouse/catalog/storage` renders, unchanged.
	 *
	 * Pairing it with the lineage graph is the workflow that earns this dock: a dataset in the DAG and
	 * the objects that actually back it, side by side, without losing either view to a navigation.
	 * Like `TablesPanel` this is a sizing wrapper over the zone's real component — no second copy.
	 */
	import ObjectBrowser from '$lib/storage/ObjectBrowser.svelte';
</script>

<div class="bg-background h-full min-h-0 overflow-auto">
	<ObjectBrowser />
</div>
