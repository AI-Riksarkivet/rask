<script lang="ts">
	import { page } from '$app/state';
	import NamespaceRegistry from '#lib/data/NamespaceRegistry.svelte';
	import { projectFilterOf } from '#lib/data/project-filter.ts';
	import { showLabel, showProject } from '#lib/data/project-filter-url.ts';
	import { labelFilterOf } from '#lib/data/labels.ts';
</script>

<svelte:head><title>Namespaces · rask</title></svelte:head>
<NamespaceRegistry
	project={projectFilterOf(page.url)}
	onprojectchange={(project) => void showProject('namespaces', page.url, project)}
	label={labelFilterOf(page.url)}
	onlabelchange={(label) => void showLabel('namespaces', page.url, label)}
/>
