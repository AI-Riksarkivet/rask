<script lang="ts">
	import '../app.css';
	import { onMount, type Snippet } from 'svelte';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import { explorerZoneNav } from '$lib/nav';
	import { descriptor } from '$lib/descriptor-store.svelte';
	import StatusBadge from '$lib/components/status-badge.svelte';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The navbar's notification bell (@rask/ui's NotificationCenter, mounted by AppShell). The shell owns
	// the surface and never fetches — the zone owns the transport — and the transport is now shared
	// (`@rask/api/runs-feed`), so a run that started, finished or FAILED reaches whoever is in this zone
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

	// The frozen /v1/me identity for the navbar, read through this zone's bearer-forwarding
	// `fetchMeViaBff` remote function (skeleton pills while in flight; null = signed out /
	// unreachable → base entries only, fail-closed on the admin surfaces). ON MOUNT, never at init:
	// the shell's identity is chrome, and a slow catalog must not hold the server-rendered page.
	// `me` arrives RESOLVED from the server layout (#107): the navbar's final entry set is in
	// the first paint — no skeleton pills, no per-hop entry swap (that swap WAS the shell flash).
	const me = $derived(data.me);

	// The sidebar is DERIVED from the active dataset, not a static list: a corpus that declares no
	// embedding spaces must not be offered an Atlas, and one with no knowledge graph must not be
	// offered Graph. `$derived` and not an effect, so switching datasets recomputes the rail rather
	// than leaving the previous corpus's areas on screen.
	/** The row table the ACTIVE searchable table reads — what the Atlas gate compares against.
	 *  Null for an unsearchable corpus or a `?table=` the corpus does not declare, which is the same
	 *  refusal the service makes rather than a silent fall-back to the default. */
	const activeRowTable = $derived(
		descriptor.view?.searchTable(page.url.searchParams.get('table'))?.row_table ?? null,
	);
	const zoneNav = $derived(explorerZoneNav(descriptor.view, activeRowTable));

	// Load the dataset descriptor once before rendering any route — every
	// renderer reads the active DatasetView, so it must be set first.
	onMount(() => {
		void descriptor.load();
	});

	// Animate soft (in-zone) navs via the View Transitions API; SSR-safe. Cross-zone
	// navs are full-document reloads (data-sveltekit-reload) and skip this.
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

<!-- The estate-shared mode-watcher owns the `.dark` class (dark-first, like every zone), so the
     navbar's theme toggle works here and a light/dark choice survives cross-zone hops (one
     localStorage key for the whole origin — the old zone-private key repainted on every hop). -->
<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- The shared estate shell: cross-zone TopNavbar up top, this zone's own routes as the zone
     sidebar (never overlapping the navbar — the sidebar lives BELOW the inset header), and the
     zone-owned service-status popover as the sidebar footer. -->
<AppShell
	pathname={page.url.pathname}
	user={data.user}
	authEnabled={data.authEnabled}
	project={data.activeProject ? { name: data.activeProject } : undefined}
	{zoneNav}
	{me}
	{notifications}
>
	{#snippet sidebarFooter()}
		<StatusBadge />
	{/snippet}
	<div class="min-h-0 flex-1 overflow-hidden">
		{#if descriptor.view}
			{@render children()}
		{:else if descriptor.error}
			<div class="grid h-full place-items-center p-6 text-center">
				<div class="max-w-md">
					<p class="text-foreground text-sm font-medium">Could not load the dataset descriptor</p>
					<p class="text-muted-foreground mt-1 text-xs">{descriptor.error}</p>
				</div>
			</div>
		{:else}
			<div class="text-muted-foreground grid h-full place-items-center text-sm">Loading dataset…</div>
		{/if}
	</div>
</AppShell>
