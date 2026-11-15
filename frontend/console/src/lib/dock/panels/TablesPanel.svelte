<script lang="ts">
	/**
	 * The catalog's table registry — literally the component `/catalog/tables` renders.
	 *
	 * `TableRegistry` reads through the shared read instances (`fetchTables`), so the panel is a sizing
	 * wrapper: the SAME component as the page, in a different frame, showing the last rows it read
	 * again at once when the dock remounts it.
	 */
	import TableRegistry from '#lib/data/TableRegistry.svelte';

	/** The panel's own filters: kept out of the URL, so choosing a project or a label keeps the reader
	 *  on the workbench. They reset when the dock remounts the panel, as the registry's search does. */
	let project = $state('');
	let label = $state('');
</script>

<div class="bg-background h-full min-h-0 overflow-auto">
	<TableRegistry
		{project}
		onprojectchange={(next) => (project = next)}
		{label}
		onlabelchange={(next) => (label = next)}
		frame="panel"
	/>
</div>
