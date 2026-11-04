<script lang="ts">
	import '../app.css';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import { onMount, type Snippet } from 'svelte';
	import { auth, expired, login } from '$lib/auth.svelte';
	import { dismiss, markSeen, readInboxFeed, readInboxState } from '$lib/live/inbox.api';
	import { areaOf, ESTATE_NAV, inLakehouse, lakehouseSidebar } from '$lib/nav';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The sidebar follows where you are: the lakehouse's grouped rail on its own pages (admin rows
	// shown DISABLED to a non-admin, #143), the estate's one-leaf nav — which renders no rail — on the
	// estate pages, and the project rail on an opened project (`projectRail` below).
	const zoneNav = $derived(
		inLakehouse(page.url.pathname) ? lakehouseSidebar(data.me?.estate_admin ?? false) : ESTATE_NAV,
	);

	// The WORKBENCH must fill a SIZED flex item rather than scroll inside an auto-height one: a dock
	// lays its grid out in PIXELS, so inside the plain `overflow-y-auto` wrapper it would size to
	// nothing and hand its scrolling to the page. Panels own their own overflow.
	const canvasArea = $derived(areaOf(page.url.pathname) === 'workbench');

	// The navbar's notification bell (@rask/ui's NotificationCenter, mounted by AppShell). The shell
	// owns the surface and never fetches — the app owns the transport, `$lib/live/inbox.api`.
	//
	// The read state: per SUBJECT for the rows the inbox holds, per TAB for the rest.
	let seen = $state<string[]>([]);
	let dismissed = $state<string[]>([]);

	// The query OBJECT, held once: `.refresh()` re-reads it and `.current` reads the value back, and
	// the previous value stays readable while the next is in flight, so the badge never flickers.
	// `null` until mounted; the panel's value `null` is the un-wired case (no session, no service).
	let inboxQuery = $state<ReturnType<typeof readInboxFeed> | null>(null);
	const inbox = $derived(inboxQuery?.current ?? null);

	onMount(() => {
		// Seeded on mount, never during render: no page may wait on the notification plane to paint.
		inboxQuery = readInboxFeed();
		// UNION, not replace — anything this tab marked while the read was in flight stays marked.
		void readInboxState()
			.then((state) => {
				// Null is the auth-off case and the outage case alike; the bell then falls back to the
				// component's own per-tab memory rather than to a blank panel.
				if (!state) return;
				seen = [...new Set([...seen, ...state.seen])];
				dismissed = [...new Set([...dismissed, ...state.dismissed])];
			})
			.catch(() => {
				/* the per-tab fallback stands — a bell that cannot persist still has to ring */
			});
	});

	// The session ends when its token does: there is no refresh token in memory, so the browser goes
	// back to the issuer, which answers at once while it still holds a session, and returns here.
	$effect(() => {
		const session = auth.session;
		if (!session || session.expiresAt === 0) return;
		const wait = Math.max(session.expiresAt * 1000 - Date.now(), 0);
		const timer = setTimeout(() => {
			if (expired(session)) void login(page.url.pathname + page.url.search);
		}, wait);
		return () => clearTimeout(timer);
	});

	// The two writes are optimistic and best-effort: the local set moves first so the badge never waits
	// on a round trip, and a refused write leaves this tab consistent with itself.
	const notifications = $derived({
		// `undefined`, never `[]`, when the inbox did not answer: an EMPTY inbox is a fact worth
		// rendering ("nothing addressed to you"), while an ABSENT one means this stack has no inbox.
		inbox: inbox?.rows,
		inboxUnread: inbox?.unread,
		seen,
		dismissed,
		onseen: (next: string[]) => {
			seen = next;
			void markSeen(next)
				// The badge is the server's count, so a write that changed something has to move it.
				.then(() => inboxQuery?.refresh())
				.catch(() => {});
		},
		ondismiss: (notificationId: string, next: string[]) => {
			dismissed = next;
			void dismiss(notificationId)
				.then(() => inboxQuery?.refresh())
				.catch(() => {});
		},
	});

	// Animate soft navs via the View Transitions API.
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
<Toaster />

{#snippet projectRail()}
	<!-- /projects/<id> is INSIDE a project, so it carries a rail — the rail is where the project
	     switcher lives. The estate-level pages keep no rail: they have nothing to navigate. -->
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
	{zoneNav}
	me={data.me}
	meLoading={false}
	sidebarContent={/^\/projects\/./.test(page.url.pathname) ? projectRail : undefined}
	{notifications}
>
	{#if canvasArea}
		<div class="canvas-scroll">
			{@render children()}
		</div>
	{:else}
		<div class="min-h-0 flex-1 overflow-y-auto">
			{@render children()}
		</div>
	{/if}
</AppShell>

<style>
	.canvas-scroll {
		display: flex;
		flex-direction: column;
		flex: 1 1 0;
		min-height: 0;
		overflow-y: auto;
	}
</style>
