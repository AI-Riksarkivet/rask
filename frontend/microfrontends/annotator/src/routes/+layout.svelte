<script lang="ts">
	import '../app.css';
	import { onMount, type Snippet } from 'svelte';
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { ANNOTATOR_ZONE_NAV } from '$lib/nav';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import type { Me } from '@rask/api';
	import { fetchMeViaBff } from '$lib/http';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The navbar's notification bell (@rask/ui's NotificationCenter, mounted by AppShell). The shell owns
	// the surface and never fetches — the zone owns the transport — and the transport is now shared
	// (`@rask/api/runs-feed`), so a run that started, finished or FAILED reaches whoever is in this zone
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

	// The estate-constant top navbar: cross-zone IA + identity, identical in every MFE. `me` comes
	// browser-side from this zone's bearer-forwarding /capi/v1/me pass-through (null = signed out /
	// unreachable → base entries only, fail-closed on the admin surfaces). The annotator's own
	// canvas shell fills the content area below.
	let me = $state<Me | null>(null);
	let meLoading = $state(true);
	onMount(async () => {
		me = await fetchMeViaBff();
		meLoading = false;
	});
</script>

<!-- The estate-shared mode-watcher owns the `.dark` class, mounted exactly as every other zone
     mounts it. That is the whole point: the theme choice lives in ONE origin-wide localStorage
     key, so the navbar's toggle works here and a light estate stays light when you hop into the
     annotator. Previously this zone pinned `class="dark"` on <html> and read its own
     `lance-media-theme` key, which is why it rendered dark against a light estate. First paint
     is handled by the boot script in app.html (this zone's canvas route is ssr=false, so the
     mode-watcher head script other zones rely on never reaches the document). -->
<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- The SHARED estate shell. `canvas` mode (icon-collapsed rail, no breadcrumb, full-height
     children) applies ONLY while the drawing canvas is actually showing (`?keys=`) — when the whole
     zone WAS one canvas page, the zone-wide `canvas` flag was right; with the projects landing and
     detail pages it made this zone the one estate zone with a cramped icon rail and no breadcrumb
     on ordinary pages, which read as "a different app". Pages get the same expanded, labeled
     sidebar every other zone renders; the canvas still keeps its width. -->
<AppShell
	pathname={page.url.pathname}
	{me}
	{meLoading}
	user={data.user}
	authEnabled={data.authEnabled}
	zoneNav={ANNOTATOR_ZONE_NAV}
	canvas={page.url.searchParams.has('keys')}
	{notifications}
>
	{@render children()}
</AppShell>
