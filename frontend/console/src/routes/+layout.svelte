<script lang="ts">
	import '../app.css';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { appearance } from '@riksarkivet/ui/appearance';
	import { APPEARANCE } from '#lib/appearance-options.ts';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '#lib/shell/index.ts';
	import { Button } from '@riksarkivet/ui/button';
	import type { Snippet } from 'svelte';
	import { auth, expired, login, logout, renew } from '#lib/auth.svelte.ts';
	import { activity } from '#lib/activity.svelte.ts';
	import { areaOf, consoleSidebar, GROUP_NAMES, PAGE_NAMES } from '#lib/nav.ts';
	import { catalogTrail } from '#lib/catalog-trail.ts';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// The one navigation: the same sidebar on every page, its estate-admin rows disabled for anyone
	// else (`#lib/nav.ts`).
	const pageNav = $derived(consoleSidebar(data.me));

	// The theme and mode: the store takes over from the boot script `app.html` loads, with the same
	// options (`#lib/appearance-options.ts`), and the returned cleanup stops it.
	$effect(() => appearance.start(APPEARANCE));

	// The WORKBENCH must fill a SIZED flex item rather than scroll inside an auto-height one: a dock
	// lays its grid out in PIXELS, so inside the plain `overflow-y-auto` wrapper it would size to
	// nothing and hand its scrolling to the page. Panels own their own overflow.
	const canvasArea = $derived(areaOf(page.url.pathname) === 'workbench');

	// A token that reaches its expiry unrenewed (the renewal a minute earlier did not land) is renewed
	// here once more; where it cannot be, `renew` marks the session ended and the notice below offers to
	// sign in again. Nothing redirects on a timer: a page the person is typing into stays theirs.
	$effect(() => {
		const session = auth.session;
		if (!session || session.expiresAt === 0) return;
		const wait = Math.max(session.expiresAt * 1000 - Date.now(), 0);
		const timer = setTimeout(() => {
			if (auth.session && expired(auth.session)) void renew();
		}, wait);
		return () => clearTimeout(timer);
	});

	const signInAgain = () => void login(page.url.pathname + page.url.search);

	// Signing out: forget the reader's activity log, drop the in-memory session, end the issuer's.
	const signOut = () => {
		activity.forget();
		void logout();
	};

	// The navbar's bell (the shell's ActivityCenter, mounted by AppShell): the outcome of every write
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

<Toaster />

<AppShell
	pathname={page.url.pathname}
	routeId={page.route.id}
	user={data.user}
	authEnabled={data.authEnabled}
	{pageNav}
	pages={PAGE_NAMES}
	groups={GROUP_NAMES}
	trail={catalogTrail(page.route.id, page.params)}
	activity={activityFeed}
	onsignout={signOut}
>
	{#if auth.ended}
		<div
			role="alert"
			class="flex flex-wrap items-center gap-3 border-b border-border bg-muted px-4 py-2 text-sm"
		>
			<p class="min-w-0 flex-1">
				Your sign-in has ended, so nothing more can be read or saved from this page. What you typed
				is still here: copy anything you want to keep, then sign in again.
			</p>
			<Button size="sm" onclick={signInAgain}>Sign in again</Button>
		</div>
	{/if}
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
