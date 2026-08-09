<script lang="ts">
	import '../app.css';
	import { onMount, type Snippet } from 'svelte';
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';

	import { ANNOTATOR_ZONE_NAV } from '$lib/nav';
	import { base } from '$app/paths';
	import { reviewSelection } from '$lib/labeling/review-selection.svelte';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
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

	// Only while the canvas is actually showing: on the landing and detail pages the path already
	// says everything, and appending a unit crumb there would invent a location.
	const crumbs = $derived.by(() => {
		if (!page.url.searchParams.has('keys')) return [];
		const out: { id: string; label: string; href: string }[] = [];
		const taskId = reviewSelection.taskId;
		if (taskId)
			out.push({ id: `task:${taskId}`, label: taskId, href: `${base}/tasks/${taskId}` });
		const unit = reviewSelection.active;
		if (unit) {
			const position =
				reviewSelection.total > 1 ? ` (${reviewSelection.index + 1}/${reviewSelection.total})` : '';
			out.push({
				id: `unit:${unit.key}`,
				label: `${unit.key}${position}`,
				href: page.url.pathname + page.url.search,
			});
		}
		return out;
	});

	// The estate-constant top navbar: cross-zone IA + identity, identical in every MFE. `me` comes
	// browser-side from this zone's bearer-forwarding /capi/v1/me pass-through (null = signed out /
	// unreachable → base entries only, fail-closed on the admin surfaces). The annotator's own
	// canvas shell fills the content area below.
	// `me` arrives RESOLVED from the server layout (#107): the navbar's final entry set is in
	// the first paint — no skeleton pills, no per-hop entry swap (that swap WAS the shell flash).
	const me = $derived(data.me);
	const meLoading = false;
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

<!-- The canvas' own crumbs. The shell derives its trail from the PATH, and this view's state is a
     QUERY (`?keys=…`), so without these the annotate view could only say "Annotator" — which is why
     it had no breadcrumb at all. The zone knows the two things the path cannot: the task it was
     opened from, and which unit of the selection is showing. The task crumb links back to its
     detail page, so the breadcrumb is also the way OUT of the canvas. -->
<!-- The SHARED estate shell, WITH its rail.

     This passed `zoneNav={null}` for a while, on the argument that two rows (Labeling tasks, Browse
     corpus) beside the annotate view's OWN annotation panel read as two sidebars competing. That
     argument was about the CANVAS, and the mechanism cost far more than it claimed: `AppShell`
     gates the WHOLE `<AppSidebar>` on `zoneNavLeaves(zoneNav).length > 1`, and the sidebar HEADER
     is where the PROJECT SWITCHER lives. So null did not remove two rows — it removed the project
     dropdown, the estate zone links and the collapse trigger, and landing in Annotate from anywhere
     else silently stripped away the navigation every other zone provides. `app-shell.svelte`
     describes having already fixed exactly that failure for `canvas` mode; the null path
     reintroduced it by another door.

     The canvas concern is handled where it belongs: a `canvas` zone starts the rail
     ICON-COLLAPSED (see `sidebarOpen` there), so the drawing surface keeps its width and the rail
     is one click away rather than absent.

     `canvas` mode (no breadcrumb, full-height children) still applies ONLY while the drawing canvas
     is showing (`?keys=`): the labeling-task landing and detail pages keep the ordinary chrome, because
     making the whole zone canvas-mode is what once made this read as "a different app". -->
<AppShell
	pathname={page.url.pathname}
	{me}
	{meLoading}
	user={data.user}
	authEnabled={data.authEnabled}
	project={data.activeProject ? { name: data.activeProject } : undefined}
	zoneNav={ANNOTATOR_ZONE_NAV}
	canvas={page.url.searchParams.has('keys')}
	{crumbs}
	{notifications}
>
	{@render children()}
</AppShell>
