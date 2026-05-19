<script lang="ts">
	import { Save, Loader2 } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';

	let {
		dirty,
		saving,
		onclick,
	}: {
		dirty: boolean;
		saving: boolean;
		onclick: () => void;
	} = $props();
</script>

<div class="flex items-center gap-3">
	<Button size="sm" disabled={!dirty || saving} {onclick}>
		{#if saving}
			<Loader2 class="h-4 w-4 animate-spin" />
			Saving...
		{:else}
			<Save class="h-4 w-4" />
			Save
		{/if}
	</Button>
	{#if dirty}
		<span class="text-xs text-muted-foreground">Unsaved changes</span>
	{/if}
</div>
