<script lang="ts">
	import { resolve } from '$app/paths';
	import '../app.css';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import type { Snippet } from 'svelte';
	import { auth, expired, login } from '#lib/auth.svelte.ts';
	import { ANONYMOUS, dismiss, markSeen, readInboxFeed } from '#lib/live/inbox.api.ts';
	import { areaOf, ESTATE_NAV, inLakehouse, lakehouseSidebar } from '#lib/nav.ts';
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
	// owns the surface and never fetches — the app owns the transport, `#lib/live/inbox.api`.
	//
	// Whom the bell reads for: the signed-in person, or, on a stack that signs nobody in, its one
	// anonymous inbox. `null` while sign-in is still completing: the inbox derives whose inbox it is
	// from the token, so a read sent before one exists is refused and would hold the bell empty.
	const reader = $derived(auth.enabled ? (auth.session?.sub ?? null) : ANONYMOUS);

	// The query OBJECT, one per person: `.refresh()` re-reads it and `.current` reads the value back,
	// and the previous value stays readable while the next is in flight, so the badge never flickers.
	// Reading it never holds a page's paint: the read starts here, and the bell fills when it answers.
	const inboxQuery = $derived(reader === null ? null : readInboxFeed(reader));
	const inbox = $derived(inboxQuery?.current ?? null);

	// The read state: what the inbox holds for this person, united with what this tab marked since
	// (UNION, not replace: a mark made while a read was in flight stays marked). The tab's own marks
	// are a writable derived of the person, so a different person starts from none of them.
	const marksOf = (_person: string | null): string[] => [];
	const union = (here: string[], held: string[] | undefined): string[] =>
		held ? [...new Set([...here, ...held])] : here;
	let seenHere = $derived(marksOf(reader));
	let dismissedHere = $derived(marksOf(reader));
	const seen = $derived(union(seenHere, inbox?.held.seen));
	const dismissed = $derived(union(dismissedHere, inbox?.held.dismissed));

	// The session ends when its token does: there is no refresh token in memory, so the browser goes
	// back to the issuer and returns here: an issuer that keeps a session answers at once, and the dev
	// Dex, which keeps none, shows its login form.
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
		inbox: inbox?.panel.rows,
		inboxUnread: inbox?.panel.unread,
		seen,
		dismissed,
		onseen: (next: string[]) => {
			seenHere = next;
			void markSeen(next)
				// The badge is the server's count, so a write that changed something has to move it.
				.then(() => inboxQuery?.refresh())
				.catch(() => {});
		},
		ondismiss: (notificationId: string, next: string[]) => {
			dismissedHere = next;
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
			href={resolve('/projects')}
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
