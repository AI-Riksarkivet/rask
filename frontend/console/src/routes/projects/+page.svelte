<script lang="ts">
	import ProjectGallery from '#lib/ProjectGallery.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
</script>

<svelte:head><title>Projects · rask</title></svelte:head>

<ProjectGallery heading="Projects" {...data} />
