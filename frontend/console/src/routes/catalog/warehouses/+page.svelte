<script lang="ts">
	import { page } from '$app/state';
	import WarehouseAdmin from '#lib/data/WarehouseAdmin.svelte';
	import { projectFilterOf } from '#lib/data/project-filter.ts';
	import { showProject } from '#lib/data/project-filter-url.ts';
</script>

<svelte:head><title>Warehouses · rask</title></svelte:head>
<WarehouseAdmin
	project={projectFilterOf(page.url)}
	onprojectchange={(project) => void showProject('warehouses', page.url, project)}
/>
