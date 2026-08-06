<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { onMount } from 'svelte';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import type { Snippet } from 'svelte';
	import { HOME_ZONE_NAV } from '$lib/nav';
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
</script>

<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- The same estate shell as every other zone (identical server-rendered chrome, so a cross-zone
     hop into "/" paints stable navbar + sidebar immediately). `me` resolved in the layout load —
     the navbar SSRs its final entry set, no skeleton pass on the landing. -->

{#snippet projectRail()}
	<!-- /projects/<id> is INSIDE a project (the two-level ruling), so it must carry the rail every
	     zone carries — the rail is where the project SWITCHER lives, and the one page that IS the
	     project losing the project control read as "the sidebar is gone" (user, 2026-08-05). The
	     estate-level pages keep no rail: they have nothing to navigate. -->
	<nav aria-label="Project" class="flex flex-col gap-0.5 px-2 py-1">
		<span class="text-muted-foreground px-2 py-1 text-xs">Project</span>
		<a
			href="/projects"
			class="text-muted-foreground hover:text-foreground hover:bg-accent/40 rounded-md px-2 py-1.5 text-sm transition-colors"
			>All projects</a
		>
	</nav>
{/snippet}

<AppShell
	pathname={page.url.pathname}
	user={data.user}
	authEnabled={data.authEnabled}
	project={data.activeProject ? { name: data.activeProject } : undefined}
	zoneNav={HOME_ZONE_NAV}
	me={data.me}
	meLoading={false}
	sidebarContent={/^\/projects\/./.test(page.url.pathname) ? projectRail : undefined}
	{notifications}
>
	<div class="min-h-0 flex-1 overflow-y-auto">
		{@render children()}
	</div>
</AppShell>
