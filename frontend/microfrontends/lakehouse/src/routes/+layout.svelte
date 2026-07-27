<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell, ForbiddenPage } from '@rask/ui/shell';
	import { base } from '$app/paths';
	import { onMount, type Snippet } from 'svelte';
	import type { Me } from '@rask/api';
	import { fetchMeViaBff } from '$lib/http';
	import { areaOf, lakehouseNav } from '$lib/nav';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The frozen /v1/me identity for the navbar, fetched browser-side through this zone's
	// bearer-forwarding BFF (skeleton pills while in flight; null = signed out / unreachable →
	// base entries only, fail-closed on the admin surfaces).
	let me = $state<Me | null>(null);
	let meLoading = $state(true);
	onMount(async () => {
		me = await fetchMeViaBff();
		meLoading = false;
	});

	const area = $derived(areaOf(page.url.pathname));

	// The navbar's notification bell (@rask/ui's NotificationCenter, mounted by AppShell). The shell owns
	// the surface and never fetches — the zone owns the transport — so this is the transport: the ONE
	// `lineageFeed` stream, which re-reads `GET /runs` when a run actually changes state and hands back
	// only the newest window, trimmed server-side (see `$lib/live/feeds.remote`). Every lineage-backed
	// panel under this layout reads its cursor off the SAME stream, so the bell and the page beneath it
	// can never be a poll apart. Mounted on the ROOT layout, so it follows the user across all four areas.
	//
	// Opened ON MOUNT, never at init: a live query touched during render makes the SERVER hold the page
	// until the feed's first value — a run-board read on the critical path of every page in the zone.
	// Same rule (and the same reason) as `$lib/live/tick.svelte`.
	//
	// `.current` is undefined until the first value lands; an empty feed and a not-yet-connected one both
	// render as "no notifications", which is the honest reading of both.
	let feed = $state<{ current: LineagePulse | undefined } | null>(null);
	onMount(() => {
		feed = lineageFeed();
	});
	const notifications = $derived({
		runs: feed?.current?.runs ?? [],
		allHref: `${base}/lineage/runs`,
	});

	// The ADMIN area's door, which used to be the admin zone's own root layout. Fail-closed on a
	// governed stack: admin content renders ONLY once /v1/me resolves WITH the estate-admin privilege —
	// an unresolved lookup shows the checking state, and null (signed out, catalog unreachable, contract
	// drift) is a denial, never a default-open. An auth-off stack has no identities to gate on (the
	// backend answers estate_admin=true for dev parity anyway), so it stays open — the same posture as
	// every panel's own 401 handling. Scoped to the area, so merging admin into this zone did not widen
	// the gate over the catalog, lineage or models routes.
	const forbidden = $derived(
		area === 'admin' && data.authEnabled && !meLoading && !(me?.estate_admin ?? false),
	);
	const checking = $derived(area === 'admin' && data.authEnabled && meLoading);
	// Don't advertise the admin area's routes to an identity the door refuses (or before the verdict).
	const zoneNav = $derived(forbidden || checking ? null : lakehouseNav(page.url.pathname));

	// The lineage area's Graph and Columns canvases set height:100% and must fill a SIZED flex item
	// rather than scroll inside an auto-height one; every other area wants the plain scroll wrapper.
	const canvasArea = $derived(area === 'lineage');

	// Animate soft navs via the View Transitions API; SSR-safe. Since the four lakehouse areas merged
	// into this one zone, a hop between them (catalog -> lineage -> models -> admin) is a soft nav and
	// gets the transition; only a hop to media/annotator/home is still a full document reload.
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

<AppShell
	pathname={page.url.pathname}
	user={data.user}
	authEnabled={data.authEnabled}
	{zoneNav}
	{me}
	{meLoading}
	{notifications}
>
	{#if forbidden}
		<ForbiddenPage
			title="Admin is estate-admin only"
			message="These surfaces span every tenant. Your identity does not hold the estate-admin privilege (can_observe_events on the FGA root)."
			home="/"
		/>
	{:else if checking}
		<!-- Fail-closed while the identity resolves: no admin content before the verdict. -->
		<div class="text-muted-foreground flex flex-1 items-center justify-center text-sm">
			Checking access…
		</div>
	{:else if canvasArea}
		<div class="zone-scroll">
			{@render children()}
		</div>
	{:else}
		<div class="min-h-0 flex-1 overflow-y-auto">
			{@render children()}
		</div>
	{/if}
</AppShell>

<style>
	.zone-scroll {
		display: flex;
		flex-direction: column;
		flex: 1 1 0;
		min-height: 0;
		overflow-y: auto;
	}
</style>
