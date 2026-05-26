<script lang="ts">
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Button } from '$lib/components/ui/button';
	import { ExternalLink, FileJson } from 'lucide-svelte';

	// FastAPI auto-generates an OpenAPI schema at /api/openapi.json plus two
	// rendered viewers (/api/docs = Swagger UI, /api/redoc = ReDoc). All three
	// are reachable same-origin through the vite proxy that forwards /api to
	// the viewer backend, so the iframe just works.
	type View = 'swagger' | 'redoc';
	let view = $state<View>('swagger');
	const iframeSrc = $derived(view === 'swagger' ? '/api/docs' : '/api/redoc');
</script>

<svelte:head>
	<title>API — RASK</title>
</svelte:head>

<RayShell title="API" flush>
	{#snippet center()}
		<div class="flex items-center gap-1 rounded-md bg-white/5 p-0.5 text-xs">
			<button
				type="button"
				class={`rounded px-2 py-0.5 transition ${
					view === 'swagger'
						? 'bg-white/15 text-white'
						: 'text-[oklch(0.78_0.005_260)] hover:bg-white/10'
				}`}
				onclick={() => (view = 'swagger')}>Swagger</button
			>
			<button
				type="button"
				class={`rounded px-2 py-0.5 transition ${
					view === 'redoc'
						? 'bg-white/15 text-white'
						: 'text-[oklch(0.78_0.005_260)] hover:bg-white/10'
				}`}
				onclick={() => (view = 'redoc')}>ReDoc</button
			>
		</div>
	{/snippet}

	{#snippet actions()}
		<Button
			variant="ghost"
			size="icon-sm"
			class="text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white"
			onclick={() => window.open('/api/openapi.json', '_blank', 'noopener')}
			title="Open openapi.json"
		>
			<FileJson class="h-4 w-4" />
		</Button>
		<Button
			variant="ghost"
			size="icon-sm"
			class="text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white"
			onclick={() => window.open(iframeSrc, '_blank', 'noopener')}
			title="Open in new tab"
		>
			<ExternalLink class="h-4 w-4" />
		</Button>
	{/snippet}

	<div class="flex flex-1 flex-col">
		<iframe
			src={iframeSrc}
			class="bg-background h-full w-full flex-1 border-0"
			title={view === 'swagger' ? 'Swagger UI' : 'ReDoc'}
			referrerpolicy="same-origin"
		></iframe>
	</div>
</RayShell>
