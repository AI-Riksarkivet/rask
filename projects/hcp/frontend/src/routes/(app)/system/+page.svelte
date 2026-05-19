<script lang="ts">
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';

	// Redirect to the first system sub-page
	onMount(() => {
		goto('/system/network', { replaceState: true });
	});
</script>

<svelte:head><title>System - HCP Admin Console</title></svelte:head>
