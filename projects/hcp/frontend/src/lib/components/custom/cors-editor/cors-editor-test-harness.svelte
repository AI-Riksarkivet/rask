<script lang="ts">
	import CorsEditor from './cors-editor.svelte';
	import { toast } from 'svelte-sonner';
	import { Toaster } from '$lib/components/ui/sonner/index.js';

	let saved = $state(false);
	let deleted = $state(false);

	async function handleSave(xml: string) {
		await new Promise((r) => setTimeout(r, 100));
		saved = true;
	}

	async function handleDelete() {
		await new Promise((r) => setTimeout(r, 100));
		deleted = true;
	}
</script>

<Toaster />

<div class="max-w-lg space-y-4 p-4">
	<CorsEditor
		corsXml={`<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>*</AllowedOrigin>
    <AllowedMethod>GET</AllowedMethod>
  </CORSRule>
</CORSConfiguration>`}
		onsave={handleSave}
		ondelete={handleDelete}
	/>

	{#if saved}
		<div data-testid="save-result" class="text-sm text-emerald-600">CORS saved</div>
	{/if}
	{#if deleted}
		<div data-testid="delete-result" class="text-sm text-destructive">CORS deleted</div>
	{/if}
</div>
