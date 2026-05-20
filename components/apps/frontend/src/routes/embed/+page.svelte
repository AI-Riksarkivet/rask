<script lang="ts">
	import { page as pageStore } from '$app/state';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Button } from '$lib/components/ui/button';
	import { ExternalLink, RotateCcw } from 'lucide-svelte';

	// /embed?path=/jobs/<id>  →  iframe src = /ray-dashboard/#/jobs/<id>
	// Same-origin via the vite (and prod FastAPI) proxy at /ray-dashboard, so the
	// user only needs the frontend port forwarded — Ray Dashboard's bundled JS
	// also makes absolute /api/v0/... and /api/jobs/... fetches that we forward
	// through. Hash routing keeps internal Ray navigation inside the iframe.
	const targetPath = $derived(pageStore.url?.searchParams.get('path') ?? '');
	const hash = $derived(
		targetPath ? `#${targetPath.startsWith('/') ? targetPath : `/${targetPath}`}` : '',
	);

	const iframeSrc = $derived(`/ray-dashboard/${hash}`);

	let iframeEl: HTMLIFrameElement | undefined = $state();
	let iframeLoaded = $state(false);

	// Imperatively set src each time iframeSrc changes. We can't rely on the
	// src attribute alone — browsers don't always treat a hash-only change as
	// navigation when the iframe is already loaded, so updating the attribute
	// on its own may leave the iframe stuck on the previous Ray view.
	$effect(() => {
		if (iframeEl && iframeEl.src !== new URL(iframeSrc, location.origin).toString()) {
			iframeLoaded = false;
			iframeEl.src = iframeSrc;
		}
	});

	function reload() {
		if (iframeEl) {
			iframeLoaded = false;
			iframeEl.src = iframeSrc;
		}
	}
</script>

<svelte:head>
	<title>Ray — ra-batch</title>
</svelte:head>

<RayShell title={`Ray ${targetPath || ''}`} flush>
	{#snippet actions()}
		<Button
			variant="ghost"
			size="icon-sm"
			class="text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white"
			onclick={reload}
			title="Reload iframe"
		>
			<RotateCcw class="h-4 w-4" />
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

	<div class="relative flex flex-1 flex-col">
		{#if !iframeLoaded}
			<div
				class="pointer-events-none absolute inset-x-0 top-0 z-10 h-0.5 progress-shimmer"
			></div>
		{/if}
		<iframe
			bind:this={iframeEl}
			src={iframeSrc}
			onload={() => (iframeLoaded = true)}
			class="h-full w-full flex-1 border-0 bg-background"
			title="Ray Dashboard"
			referrerpolicy="same-origin"
		></iframe>
	</div>
</RayShell>
