<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();

	// Animate soft client-side navs via the View Transitions API. onNavigate
	// registers a callback and is SSR-safe; the document check guards browsers
	// without support. Cross-document MFE navs are handled by the `@view-transition`
	// rule in the shared @rask/ui tokens stylesheet.
	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		return new Promise((resolve) => {
			document.startViewTransition(async () => {
				resolve();
				await navigation.complete;
			});
		});
	});
</script>

<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- Shared shell from @rask/ui — identical chrome across every microfrontend.
     Ray health is a live signal at the top of the compute overview, not in the shell. -->
<AppShell pathname={page.url.pathname}>
	{@render children()}
</AppShell>
