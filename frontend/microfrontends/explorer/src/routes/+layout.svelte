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
	import { dismiss, markSeen, readInboxFeed, readInboxState } from '$lib/live/inbox.remote';
	import { explorerZoneNav } from '$lib/nav';
	import { descriptor } from '$lib/descriptor-store.svelte';
	import StatusBadge from '$lib/components/status-badge.svelte';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The navbar's notification bell (@rask/ui's NotificationCenter, mounted by AppShell). The shell owns
	// the surface and never fetches — the zone owns the transport — and the transport is now shared
	// (`@rask/api/runs-feed`), so a run that started, finished or FAILED reaches whoever is in this zone
	let feed = $state<{ current: LineagePulse | undefined } | null>(null);

	// The read state the bell binds: per SUBJECT for the rows the inbox holds (persisted through
	// `$lib/live/inbox.remote`, one inbox actor per subject), per TAB for everything else.
	let seen = $state<string[]>([]);
	let dismissed = $state<string[]>([]);

	// The Inbox tab's rows. `null` = un-wired (no session, no notifications service) and the bell falls
	// back to a single run list with no tabs — the auth-off dev shape this zone is usually run in.
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
		// Seeded ON MOUNT, never during render: a live/remote read touched while rendering makes the
		// server hold the page until the notification plane answers.
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

	// `.current` is undefined until the first value lands; an empty feed and a not-yet-connected one both
	// render as "no notifications", which is the honest reading of both.
	//
	// The two writes are optimistic: the local set moves first so the badge never waits on a round trip,
	// and a refused write leaves this tab consistent with itself (repaired on the next reload, where the
	// seeded set is missing it and the following close sends it again).
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
