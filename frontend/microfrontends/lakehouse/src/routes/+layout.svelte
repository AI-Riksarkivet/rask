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
	import { areaOf, lakehouseSidebar } from '$lib/nav';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import { dismiss, markSeen, readInboxFeed, readInboxState } from '$lib/live/inbox.remote';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The frozen /v1/me identity for the navbar, fetched browser-side through this zone's
	const me = $derived(data.me);

	const area = $derived(areaOf(page.url.pathname));
	// The area behind the estate-admin door. `governance` was the other one until #105 moved Access and
	// Audit to the home zone's `/settings/` — this zone no longer serves that segment at all, so listing
	// it here would gate a path that cannot be reached.
	const PRIVILEGED_AREAS = new Set(['admin']);

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

	// The bell's read state: per SUBJECT for the rows the inbox holds, per TAB for the rest. The
	// component exposes both as bindable with `onseen`/`ondismiss` as "the persistence seam"; this is
	// the zone giving that seam its backend (`$lib/live/inbox.remote`).
	let seen = $state<string[]>([]);
	let dismissed = $state<string[]>([]);
	// The panel's Inbox tab renders THESE, not the run rows. `null` = un-wired (no session, no
	// service) and the bell falls back to a single run list with no tabs — the auth-off dev shape.
	// The query OBJECT, held once — not a $state snapshot refilled by re-calling the query.
	// Re-calling a remote `query()` hands back the CACHED value, so a `readInboxFeed()` after a write
	// re-read exactly what was already there and the badge never moved. `.refresh()` is the only thing
	// that re-fetches, and `.current` is how its value is read back — the estate's one polling idiom
	// (`compute/src/routes/+page.svelte`), which is also flicker-free because the previous value stays
	// readable while the next is in flight.
	let inboxQuery = $state<ReturnType<typeof readInboxFeed> | null>(null);
	const inbox = $derived(inboxQuery?.current ?? null);

	onMount(() => {
		feed = lineageFeed();
		// Seeded on mount, never during render, and for the same reason the feed is: no page in this
		// zone may wait on the notification plane to paint.
		inboxQuery = readInboxFeed();
		// UNION, not replace — anything this tab marked while the read was in flight stays marked.
		void readInboxState()
			.then((state) => {
				// Null is the auth-off dev case and the outage case alike; the bell then falls back to
				// the component's own per-tab memory rather than to a blank panel.
				if (!state) return;
				seen = [...new Set([...seen, ...state.seen])];
				dismissed = [...new Set([...dismissed, ...state.dismissed])];
			})
			.catch(() => {
				/* the per-tab fallback stands — a bell that cannot persist still has to ring */
			});
	});

	// The two writes are optimistic and best-effort: the local set moves first so the badge never waits
	// on a round trip, and a refused write leaves this tab consistent with itself. A write lost to a
	// restart surfaces on the next RELOAD, where the seeded set is missing it and the following close
	// sends it again.
	const notifications = $derived({
		runs: feed?.current?.runs ?? [],
		// `undefined`, never `[]`, when the inbox did not answer: an EMPTY inbox is a fact worth
		// rendering ("nothing addressed to you"), while an ABSENT one means this stack has no inbox at
		// all and the bell must not claim otherwise by showing an empty Inbox tab.
		inbox: inbox?.rows,
		inboxUnread: inbox?.unread,
		seen,
		dismissed,
		onseen: (next: string[]) => {
			seen = next;
			void markSeen(next)
				// The badge is the server's count, so a write that changed something has to move it —
				// otherwise the row greys out and the number beside it does not.
				.then(() => inboxQuery?.refresh())
				.catch(() => {});
		},
		ondismiss: (notificationId: string, next: string[]) => {
			dismissed = next;
			void dismiss(notificationId)
				.then(() => inboxQuery?.refresh())
				.catch(() => {});
		},
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
		PRIVILEGED_AREAS.has(area) && data.authEnabled && !(me?.estate_admin ?? false),
	);
	// Don't advertise the admin area's routes to an identity the door refuses (or before the verdict)
	// — but hide only THOSE groups. Nulling the whole sidebar (what this did while each area had its
	// own) would now blank catalog, lineage and models too, punishing a user for visiting a URL they
	// were denied.
	const zoneNav = $derived(lakehouseSidebar(!data.authEnabled || (me?.estate_admin ?? false)));

	// The lineage area's Graph and Columns canvases set height:100% and must fill a SIZED flex item
	// rather than scroll inside an auto-height one; every other area wants the plain scroll wrapper.
	//
	// The WORKBENCH is the same shape, and belongs here for a second reason. A dock lays its grid out
	// in PIXELS, so inside the plain `overflow-y-auto` wrapper it would both size to nothing (its
	// `flex: 1 1 0` resolving against a block parent — the exact defect that made the explorer's dock
	// mount three groups and paint none) and hand its scrolling to the PAGE, which would scroll the
	// whole dock rather than the panel under the cursor. Panels own their own overflow.
	const canvasArea = $derived(area === 'lineage' || area === 'workbench');

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
	project={data.activeProject ? { name: data.activeProject } : undefined}
	{zoneNav}
	{me}
	{notifications}
>
	{#if forbidden}
		<ForbiddenPage
			title="Admin is estate-admin only"
			message="These surfaces span every tenant. Your identity does not hold the estate-admin privilege (can_observe_events on the FGA root)."
			home="/"
		/>
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
