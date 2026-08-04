<script lang="ts">
	/**
	 * The catalog's table registry — literally the component `/lakehouse/catalog/tables` renders.
	 *
	 * `TableRegistry` takes no props and owns its own reads, so the panel is a sizing wrapper and
	 * nothing else. That is the whole point of an in-zone dock: no mirrored second implementation to
	 * drift, no transport between the page and the panel — the SAME component, in a different frame.
	 */
	import TableRegistry from '$lib/data/TableRegistry.svelte';
</script>

<div class="bg-background h-full min-h-0 overflow-auto">
	<TableRegistry />
</div>
