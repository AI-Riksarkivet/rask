<script lang="ts">
	import ProjectGallery from '$lib/ProjectGallery.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
</script>

<svelte:head><title>lance</title></svelte:head>

<!-- The estate landing and `/projects` render the SAME component; only the heading differs. `data`
     carries the gallery (from $lib/gallery) plus the layout's `authEnabled`. -->
<ProjectGallery heading="lance" {...data} />
