<script lang="ts">
	import type { LayoutServerData } from './$types';
	import '../app.css';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { onMount, type Snippet } from 'svelte';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import { dismiss, markSeen, readInboxFeed, readInboxState } from '$lib/live/inbox.remote';
	import { MODELS_ZONE_NAV } from '$lib/nav';
	let { children, data }: { children: Snippet; data: LayoutServerData } = $props();

	// The navbar's notification bell (@rask/ui's NotificationCenter, mounted by AppShell). The shell owns
	// the surface and never fetches — the zone owns the transport — and the transport is the shared
	// `@rask/api/runs-feed`, so a run that started, finished or FAILED reaches whoever is in this zone
	// rather than only whoever happens to be on the run board. Opened ON MOUNT, never at init: a live
	// query touched during render makes the SERVER hold the page until the feed's first value.
	let feed = $state<{ current: LineagePulse | undefined } | null>(null);

	// The read state: per SUBJECT for the rows the inbox holds, per TAB for the rest. The shared bell
	// has always exposed these two sets as bindable with `onseen`/`ondismiss` documented as "the
	// persistence seam"; this is the zone giving that seam its backend (`services/notifications`, one
	// inbox actor per subject, reached through the gateway's `/api/notifications` row).
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
		// Seeded on mount, never during render: a live/remote read touched during render makes the
		// SERVER hold the page until its first value.
		inboxQuery = readInboxFeed();
		// UNION, not replace — anything this tab marked while the read was in flight stays marked.
		void readInboxState()
			.then((state) => {
				// Null is the auth-off dev case and the outage case alike; the bell falls back to the
				// component's own per-tab memory rather than to a blank panel.
				if (!state) return;
				seen = [...new Set([...seen, ...state.seen])];
				dismissed = [...new Set([...dismissed, ...state.dismissed])];
			})
			.catch(() => {
				/* the per-tab fallback stands — a bell that cannot persist still has to ring */
			});
	});

	// `.current` is undefined until the first value lands; an empty feed and a not-yet-connected one both
	// render as "no notifications", which is the honest reading of both.
	//
	// The two writes are optimistic and best-effort: the local set moves first so the badge never waits
	// on a round trip, and a refused write leaves this tab consistent with itself (it is re-sent on the
	// next close that has something NEW to mark, and after a reload where the seeded set is missing it).
	const notifications = $derived({
		runs: feed?.current?.runs ?? [],
		// `undefined`, never `[]`, when the inbox did not answer: an EMPTY inbox is a fact worth
		// rendering ("nothing addressed to you"), while an ABSENT one means this stack has no inbox to
		// speak of and the bell must not claim otherwise by showing an empty Inbox tab.
		inbox: inbox?.rows,
		inboxUnread: inbox?.unread,
		seen,
		dismissed,
		onseen: (next: string[]) => {
			seen = next;
			void markSeen(next)
				// The badge is the server's count, so a write that changed something has to move it.
				// Without this refresh the row greys out and the number beside it does not, which reads
				// as a broken badge rather than a stale one.
				.then(() => inboxQuery?.refresh())
				.catch(() => {});
		},
		ondismiss: (notificationId: string, next: string[]) => {
			dismissed = next;
			void dismiss(notificationId)
				.then(() => inboxQuery?.refresh())
				.catch(() => {});
		},
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
	user={data.user}
	authEnabled={data.authEnabled}
	me={data.me}
	meLoading={false}
	project={data.activeProject ? { name: data.activeProject } : undefined}
	zoneNav={MODELS_ZONE_NAV}
	{notifications}
>
	<div class="min-h-0 flex-1 overflow-y-auto">
		{@render children()}
	</div>
</AppShell>
