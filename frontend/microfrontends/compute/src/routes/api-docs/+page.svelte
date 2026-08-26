<script lang="ts">
	import { ExternalLink, FileJson } from '@lucide/svelte';

	// FastAPI auto-generates an OpenAPI schema at /api/openapi.json plus two
	// rendered viewers (/api/docs = Swagger UI, /api/redoc = ReDoc). All three
	// are reachable same-origin through the vite proxy that forwards /api to
	// the viewer backend, so the iframe just works.
	type View = 'swagger' | 'redoc';
	let view = $state<View>('swagger');
	const iframeSrc = $derived(view === 'swagger' ? '/api/docs' : '/api/redoc');
</script>

<svelte:head>
	<title>API — rask</title>
</svelte:head>

<main class="bg-background flex flex-1 overflow-hidden">
	<div class="flex flex-1 flex-col">
		<div class="flex items-center gap-2 border-b px-3 py-2">
			<div class="bg-muted/40 flex items-center gap-1 rounded-md p-0.5 text-xs">
				<button
					type="button"
					class={`rounded px-2 py-0.5 transition ${
						view === 'swagger'
							? 'bg-background text-foreground'
							: 'text-muted-foreground hover:bg-muted'
					}`}
					onclick={() => (view = 'swagger')}>Swagger</button
				>
				<button
					type="button"
					class={`rounded px-2 py-0.5 transition ${
						view === 'redoc'
							? 'bg-background text-foreground'
							: 'text-muted-foreground hover:bg-muted'
					}`}
					onclick={() => (view = 'redoc')}>ReDoc</button
				>
			</div>
			<div class="ml-auto flex items-center gap-1">
				<button
					type="button"
					class="hover:bg-muted text-muted-foreground hover:text-foreground inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors"
					onclick={() => window.open('/api/openapi.json', '_blank', 'noopener')}
					title="Open openapi.json"
				>
					<FileJson class="h-4 w-4" />
				</button>
				<button
					type="button"
					class="hover:bg-muted text-muted-foreground hover:text-foreground inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors"
					onclick={() => window.open(iframeSrc, '_blank', 'noopener')}
					title="Open in new tab"
				>
					<ExternalLink class="h-4 w-4" />
				</button>
			</div>
		</div>
		<iframe
			src={iframeSrc}
			class="bg-background h-full w-full flex-1 border-0"
			title={view === 'swagger' ? 'Swagger UI' : 'ReDoc'}
			referrerpolicy="same-origin"
		></iframe>
	</div>
</main>
