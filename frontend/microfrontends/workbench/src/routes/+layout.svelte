<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { onMount, type Snippet } from 'svelte';
	import { ViewSidebar } from '@rask/dockview/views';
	import { bench } from '$lib/bench.svelte';
	import { WORKBENCH_ZONE_NAV } from '$lib/nav';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The navbar bell. Opened ON MOUNT, never at init: a live query touched during render makes the
	// SERVER hold the page until the feed's first value.
	let feed = $state<{ current: LineagePulse | undefined } | null>(null);
	onMount(() => {
		feed = lineageFeed();
	});
	const notifications = $derived({
		runs: feed?.current?.runs ?? [],
		allHref: '/lakehouse/lineage/runs',
	});
</script>

<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<AppShell
	pathname={page.url.pathname}
	zoneNav={WORKBENCH_ZONE_NAV}
	{notifications}
	user={data.user}
	authEnabled={data.authEnabled}
	sidebarContent={viewsRail}
>
	{@render children()}
</AppShell>

{#snippet viewsRail()}
	<!-- Browser-only: the views model exists once the page has built the dock. -->
	{#if browser && bench.views !== null && bench.apply !== null}
		<ViewSidebar views={bench.views} onselect={bench.apply} rail />
	{/if}
{/snippet}
