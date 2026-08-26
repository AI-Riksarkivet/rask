<script lang="ts">
	import { page } from '$app/state';
	import TableDetail from '$lib/data/TableDetail.svelte';

	const table = $derived(page.params.table ?? '');
</script>

<svelte:head><title>{table} · rask</title></svelte:head>
<TableDetail {table} />
