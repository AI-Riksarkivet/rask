<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { onMount } from 'svelte';
	import { lineageFeed, type LineagePulse } from '$lib/live/feeds.remote';
	import { dismiss, markSeen, readInboxFeed, readInboxState } from '$lib/live/inbox.remote';
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

	// The read state: per SUBJECT for the rows the inbox holds, per TAB for the rest. The component has
	// always exposed these two sets as bindable with `onseen`/`ondismiss` documented as "the persistence
	// seam"; this is the zone giving that seam its backend (`services/notifications`, one inbox actor
	// per subject).
	//
	// THE BOUND, and it is wider than it looks because no gate can see it. The rows below come from
	// `GET /runs`, governed by DATASET visibility — every run whose outputs you can `can_get_metadata`,
	// whoever ran it. The inbox is filled by AUTHORSHIP (D4's v1 targeting: `audience_for` is
	// `(notice.author,)`) and only on terminal states. So a mover's FAILED run you can see but did not
	// start has no pointer in your inbox: marking it read persists nothing and it is unread again after
	// a reload, exactly as before S1. S3 is what closes that — it makes the panel render inbox rows
	// instead of run rows, so the two sets stop being different sets.
	let seen = $state<string[]>([]);
	let dismissed = $state<string[]>([]);

	// S3: the panel's Inbox tab renders THESE, not the run rows. `null` = un-wired (no session, no
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
		inboxQuery = readInboxFeed();
		// Seeded on mount, never during render, and for the same reason the feed is: the landing page
		// must not wait on the notification plane to paint. UNION, not replace — anything this tab has
		// already marked while the read was in flight stays marked.
		void readInboxState()
			.then((state) => {
				// Null is the auth-off dev case and the outage case alike: no session, no service, and
				// the bell falls back to the component's own per-tab memory rather than to a blank panel.
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
	// on a round trip, and a refused write leaves this tab consistent with itself.
	//
	// A refused write is retried on the next close that has something NEW to mark, and not before —
	// the component returns early when the union does not grow (`notification-center.svelte` markSeen),
	// so re-closing an unchanged panel sends nothing. Re-sending is safe when it does happen (the full
	// seen set travels every time and the service reports `updated: 0` for a row already read); what it
	// is not is a repair loop. A write lost to a restart therefore surfaces on the next RELOAD, where
	// the seeded set is missing it and the following close sends it again.
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
