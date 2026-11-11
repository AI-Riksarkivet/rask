<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import { Separator } from '../components/separator/index.js';
	import AppSidebar from './app-sidebar.svelte';
	import NavbarUser from './navbar-user.svelte';
	import ActivityCenter from './activity-center.svelte';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { IsMobile, SHELL_COLLAPSE_BREAKPOINT } from '../hooks/is-mobile.svelte.js';
	import { ChevronRight, MoreHorizontal } from '@lucide/svelte';
	import { gsap } from 'gsap';
	import type { ActivityFeed, NavUser, PageNav } from './nav-config.js';
	import { collapseCrumbs, pathCrumbs } from './breadcrumb.js';

	// Subtle content settle-in. Runs once when the shell MOUNTS — i.e. on a fresh
	// document load, which is every full-page landing — so the page gently
	// rises + fades in instead of snapping after the hard nav. Only the content area
	// animates; the sidebar/shell stay put. In-app soft navs don't remount the shell, so
	// this does NOT replay on every click (kept subtle, not annoying); those keep each
	// app's onNavigate view transition. browser-only: the wrapper starts at opacity:0 (CSS
	// below) so the first paint never shows content pre-animation (no flash); GSAP fades
	// it in and clears the transform so nothing lingers (no containing block for fixed/
	// portaled children). Reduced-motion: no animation, just reveal.
	function contentEnter(el: HTMLElement) {
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
			el.style.opacity = '1';
			return;
		}
		const tween = gsap.fromTo(
			el,
			{ y: 8 },
			{ opacity: 1, y: 0, duration: 0.32, ease: 'power2.out', clearProps: 'transform' },
		);
		return () => tween.kill();
	}

	// The shared application shell: the console's one navigation, a sidebar of every page's routes,
	// beside a content inset headed by one slim row — the rail's toggle, the breadcrumb, the bell and
	// the account menu. `pathname` and `routeId` come from the app's `$app/state` and drive the
	// breadcrumb and the rail's active row.
	let {
		pathname = '',
		routeId = null,
		// No fabricated default: a page that passes no `user` renders the signed-out states, never an
		// account named after the product.
		user = null,
		authEnabled = false,
		pageNav = null,
		activity = null,
		children,
	}: {
		pathname?: string;
		/** The route `pathname` matched (`$app/state`'s `page.route.id`): the breadcrumb prints the
		 *  segments it declares as parameters (catalog names) exactly as written. */
		routeId?: string | null;
		/** `null` = no resolved identity: signed out on an auth-on stack, or auth off entirely. */
		user?: NavUser | null;
		authEnabled?: boolean;
		/** The sidebar's routes; null renders an empty rail. */
		pageNav?: PageNav | null;
		/** The reader's activity for the bell (see `ActivityFeed`). The shell never stores it; `null`
		 *  renders no bell at all. */
		activity?: ActivityFeed | null;
		children: Snippet;
	} = $props();

	// The breadcrumb is the route's own trail: every path segment is a crumb, a project's page reads
	// Projects then the project.
	const crumbs = $derived(pathCrumbs(pathname, routeId));
	// A deep path does not fit a narrow row, so the MIDDLE of the trail folds behind an ellipsis menu
	// rather than squeezing every crumb into illegibility — and the current page, the one crumb worth
	// keeping, is never the thing that disappears. Same breakpoint the rest of the shell folds on.
	const narrow = new IsMobile(SHELL_COLLAPSE_BREAKPOINT);
	const trail = $derived(collapseCrumbs(crumbs, narrow.current ? 2 : 4));
	// Every crumb but the last is a link back up its own path; the last IS the current page, so it
	// renders as text and carries aria-current instead.
	const lastCrumbId = $derived(trail.tail.at(-1)?.id);
	// A separator goes BETWEEN crumbs, so the first one has none.
	const firstCrumbId = $derived(crumbs[0]?.id);

	// Bound, so the trigger and cmd/ctrl+b open and close the rail.
	let sidebarOpen = $state(true);
</script>

<Sidebar.Provider bind:open={sidebarOpen} class="h-svh overflow-hidden">
	<!-- The one navigation, on every page. -->
	<AppSidebar {pathname} {pageNav} />
	<Sidebar.Inset class="flex min-w-0 flex-col overflow-hidden">
		<!-- STATIC on purpose: a positioned header becomes the containing block of whatever chrome it
		     opens. A page whose content paints over chrome lowers itself (the workbench's dock isolates
		     its own stacking context); the shared shell is never raised. -->
		<header class="border-border/60 flex h-12 min-w-0 shrink-0 items-center gap-2 border-b px-4">
			<Sidebar.Trigger class="text-muted-foreground hover:text-foreground -ml-1 shrink-0" />
			<Separator orientation="vertical" class="data-[orientation=vertical]:h-4" />
			<!-- The breadcrumb takes the row's free width; a trail too long to fit folds its MIDDLE
			     behind the ellipsis menu (`collapseCrumbs`) instead of shrinking the current page. -->
			<nav aria-label="Breadcrumb" class="flex min-w-0 flex-1 items-center overflow-hidden text-sm">
				<ol class="flex min-w-0 items-center gap-1.5">
					{#each trail.lead as crumb (crumb.id)}
						<li class="flex min-w-0 shrink-0 items-center gap-1.5">
							<a
								href={crumb.href}
								class="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 truncate rounded-sm transition-colors outline-none focus-visible:ring-3"
								>{crumb.label}</a
							>
						</li>
					{/each}
					{#if trail.hidden.length > 0}
						<!-- The folded middle: an affordance, not a deletion — every skipped ancestor is one
					     click away, so the trail stays navigable at any width. -->
						<li class="flex shrink-0 items-center gap-1.5">
							<ChevronRight class="text-muted-foreground/40 size-3.5 shrink-0" />
							<DropdownMenu.Root>
								<DropdownMenu.Trigger>
									{#snippet child({ props })}
										<button
											{...props}
											type="button"
											data-slot="breadcrumb-ellipsis"
											aria-label="Show hidden breadcrumbs"
											class="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 flex size-5 items-center justify-center rounded-sm transition-colors outline-none focus-visible:ring-3"
										>
											<MoreHorizontal class="size-3.5" />
										</button>
									{/snippet}
								</DropdownMenu.Trigger>
								<DropdownMenu.Content class="min-w-40 rounded-lg" side="bottom" align="start">
									{#each trail.hidden as crumb (crumb.id)}
										<DropdownMenu.Item class="p-0">
											<a href={crumb.href} class="w-full truncate px-2 py-1.5">{crumb.label}</a>
										</DropdownMenu.Item>
									{/each}
								</DropdownMenu.Content>
							</DropdownMenu.Root>
						</li>
					{/if}
					{#each trail.tail as crumb (crumb.id)}
						<li class="flex min-w-0 items-center gap-1.5">
							{#if crumb.id !== firstCrumbId}
								<ChevronRight class="text-muted-foreground/40 size-3.5 shrink-0" />
							{/if}
							{#if crumb.id === lastCrumbId}
								<span
									aria-current="page"
									class="text-foreground truncate font-medium"
									data-slot="breadcrumb-page">{crumb.label}</span
								>
							{:else}
								<!-- An ancestor: `collapseCrumbs` puts the whole trail in `tail` when it fits,
								     so every crumb but the current page is a link here too. -->
								<a
									href={crumb.href}
									class="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 truncate rounded-sm transition-colors outline-none focus-visible:ring-3"
									>{crumb.label}</a
								>
							{/if}
						</li>
					{/each}
				</ol>
			</nav>
			<div class="ml-auto flex shrink-0 items-center">
				{#if activity}
					<ActivityCenter
						entries={activity.entries}
						unread={activity.unread}
						onopen={activity.onopen}
						onclear={activity.onclear}
					/>
				{/if}
				<NavbarUser {user} {authEnabled} {pathname} />
			</div>
		</header>
		<div
			class="content-enter flex min-h-0 w-full min-w-0 flex-1 flex-col overflow-hidden"
			{@attach contentEnter}
		>
			{@render children()}
		</div>
	</Sidebar.Inset>
</Sidebar.Provider>

<style>
	/* Start hidden so a fresh-document first paint never shows content before the GSAP
	   contentEnter fades it in (no flash). The app renders in the browser only, so an
	   initial opacity:0 is safe; GSAP sets inline opacity:1 once it runs, which
	   overrides this. Reduced-motion users get it visible immediately. */
	.content-enter {
		opacity: 0;
	}
	@media (prefers-reduced-motion: reduce) {
		.content-enter {
			opacity: 1;
		}
	}
</style>
