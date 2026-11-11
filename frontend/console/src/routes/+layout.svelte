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
	import { activity } from '#lib/activity.svelte.ts';
	import { areaOf, ESTATE_NAV, inLakehouse, lakehouseSidebar } from '#lib/nav.ts';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The sidebar follows where you are: the lakehouse's grouped rail on its own pages (admin rows
	// shown DISABLED to a non-admin, #143), the estate's one-leaf nav — which renders no rail — on the
	// estate pages, and the project rail on an opened project (`projectRail` below).
	const pageNav = $derived(
		inLakehouse(page.url.pathname) ? lakehouseSidebar(data.me?.estate_admin ?? false) : ESTATE_NAV,
	);

	// The WORKBENCH must fill a SIZED flex item rather than scroll inside an auto-height one: a dock
	// lays its grid out in PIXELS, so inside the plain `overflow-y-auto` wrapper it would size to
	// nothing and hand its scrolling to the page. Panels own their own overflow.
	const canvasArea = $derived(areaOf(page.url.pathname) === 'workbench');

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

	// The navbar's bell (@rask/ui's ActivityCenter, mounted by AppShell): the outcome of every write
	// this console sent for the reader, kept in their browser (`#lib/activity.svelte.ts`). The shell
	// renders it and calls back when the panel opens and when the reader clears the log.
	const activityFeed = $derived({
		entries: activity.entries,
		unread: activity.unread,
		onopen: () => activity.markRead(),
		onclear: () => activity.clear(),
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
	routeId={page.route.id}
	user={data.user}
	authEnabled={data.authEnabled}
	project={data.activeProject ? { name: data.activeProject } : undefined}
	{pageNav}
	me={data.me}
	meLoading={false}
	sidebarContent={/^\/projects\/./.test(page.url.pathname) ? projectRail : undefined}
	activity={activityFeed}
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
