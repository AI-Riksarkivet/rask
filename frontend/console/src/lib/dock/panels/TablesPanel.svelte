<script lang="ts">
	/**
	 * The catalog's table registry — literally the component `/catalog/tables` renders.
	 *
	 * `TableRegistry` takes no props and reads through the shared read instances (`fetchTables`), so
	 * the panel is a sizing wrapper and nothing else: the SAME component as the page, in a different
	 * frame, showing the last rows it read again at once when the dock remounts it.
	 */
	import TableRegistry from '#lib/data/TableRegistry.svelte';
</script>

<div class="bg-background h-full min-h-0 overflow-auto">
	<TableRegistry />
</div>
