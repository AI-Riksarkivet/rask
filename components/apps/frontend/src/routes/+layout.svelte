<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();

	// Animate soft client-side navs (e.g. / <-> /<project>) via the View Transitions
	// API. onNavigate registers a callback and is SSR-safe; the document check guards
	// browsers without support. Cross-document MFE navs are handled by the
	// `@view-transition` rule in the shared @rask/ui tokens stylesheet.
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
<!-- Toaster touches browser-only APIs; render it client-side only to keep SSR clean. -->
{#if browser}
	<Toaster />
{/if}

<!-- Global providers only. This catch-all host renders just the bare `/` picker
     (no sidebar) and 307-redirects /<project> into the overview zone. The grouped
     @rask/ui AppShell sidebar is NOT rendered here — each domain MFE renders it
     identically in its own root layout (§5/§6), so you only see the sidebar once a
     cross-zone nav lands you inside a domain app (/<project>/<domain>). -->
{@render children()}
