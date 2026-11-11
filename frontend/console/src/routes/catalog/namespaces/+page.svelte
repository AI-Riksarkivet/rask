<script lang="ts">
	import { page } from '$app/state';
	import NamespaceRegistry from '#lib/data/NamespaceRegistry.svelte';
	import { projectFilterOf } from '#lib/data/project-filter.ts';
	import { showProject } from '#lib/data/project-filter-url.ts';
</script>

<svelte:head><title>Namespaces · rask</title></svelte:head>
<NamespaceRegistry
	project={projectFilterOf(page.url)}
	onprojectchange={(project) => void showProject('namespaces', page.url, project)}
/>
