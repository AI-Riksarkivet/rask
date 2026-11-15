<script lang="ts">
	import { page } from '$app/state';
	import TableDetail from '#lib/data/TableDetail.svelte';
	import { catalogPath } from '#lib/catalog-trail.ts';

	const table = $derived(page.params.table ?? '');
</script>

<svelte:head><title>{catalogPath(table)} · tables · rask</title></svelte:head>
<TableDetail {table} />
