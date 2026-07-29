<script lang="ts">
	import { page } from '$app/state';
	import TableDetail from '$lib/data/TableDetail.svelte';

	const table = $derived(page.params.table ?? '');
</script>

<svelte:head><title>{table} · lance</title></svelte:head>
<TableDetail {table} />
