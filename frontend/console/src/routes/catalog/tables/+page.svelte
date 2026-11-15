<script lang="ts">
	import { page } from '$app/state';
	import TableRegistry from '#lib/data/TableRegistry.svelte';
	import { projectFilterOf } from '#lib/data/project-filter.ts';
	import { showLabel, showProject } from '#lib/data/project-filter-url.ts';
	import { labelFilterOf } from '#lib/data/labels.ts';
</script>

<svelte:head><title>Tables · rask</title></svelte:head>
<TableRegistry
	project={projectFilterOf(page.url)}
	onprojectchange={(project) => void showProject('tables', page.url, project)}
	label={labelFilterOf(page.url)}
	onlabelchange={(label) => void showLabel('tables', page.url, label)}
/>
