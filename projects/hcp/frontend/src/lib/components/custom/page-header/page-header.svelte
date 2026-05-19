<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		title,
		description,
		actions,
	}: {
		title: string;
		description: string;
		actions?: Snippet;
	} = $props();
</script>

<div class="flex items-center justify-between">
	<div>
		<h2 class="text-2xl font-bold">{title}</h2>
		<p class="mt-1 text-sm text-muted-foreground">{description}</p>
	</div>
	{#if actions}
		{@render actions()}
	{/if}
</div>
