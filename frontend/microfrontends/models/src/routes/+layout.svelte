<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { onMount, type Snippet } from 'svelte';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import { MODELS_ZONE_NAV } from '$lib/nav';
	let { children, data }: { children: Snippet; data: { activeProject: string } } = $props();

	// The navbar's notification bell (@rask/ui's NotificationCenter, mounted by AppShell). The shell owns
	// the surface and never fetches — the zone owns the transport — and the transport is the shared
	// `@rask/api/runs-feed`, so a run that started, finished or FAILED reaches whoever is in this zone
	// rather than only whoever happens to be on the run board. Opened ON MOUNT, never at init: a live
	// query touched during render makes the SERVER hold the page until the feed's first value.
	let feed = $state<{ current: LineagePulse | undefined } | null>(null);
	onMount(() => {
		feed = lineageFeed();
	});
	// `.current` is undefined until the first value lands; an empty feed and a not-yet-connected one both
	// render as "no notifications", which is the honest reading of both.
	const notifications = $derived({
		runs: feed?.current?.runs ?? [],
		allHref: '/lakehouse/lineage/runs',
	});

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

<!-- ModeWatcher sets `class="dark"` on <html> so the .dark theme tokens apply —
     without it the shared sidebar renders unstyled. Identical wiring to every
     other microfrontend (compute/studio/monolith). -->
<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- The shared AppShell (one grouped sidebar) from @rask/ui — identical to every
     other microfrontend, zero drift. `base` (=/models) frames the breadcrumb;
     `zoneNav` renders THIS zone's areas as the sidebar leaves. -->
<AppShell
	pathname={page.url.pathname}
	project={data.activeProject ? { name: data.activeProject } : undefined}
	zoneNav={MODELS_ZONE_NAV}
	{notifications}
>
	<div class="min-h-0 flex-1 overflow-y-auto">
		{@render children()}
	</div>
</AppShell>
