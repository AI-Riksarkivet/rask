<script lang="ts">
	import { onMount, type Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import { Separator } from '../components/separator/index.js';
	import AppSidebar from './app-sidebar.svelte';
	import TopNavbar from './top-navbar.svelte';
	import ProjectSwitcher from './project-switcher.svelte';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { IsMobile, SHELL_COLLAPSE_BREAKPOINT } from '../hooks/is-mobile.svelte.js';
	import { ChevronRight, MoreHorizontal } from '@lucide/svelte';
	import { gsap } from 'gsap';
	import type { Me, NotificationFeed, Project, NavUser, ZoneNav } from './nav-config.js';
	import { collapseCrumbs, pathCrumbs, projectFromHost } from './breadcrumb.js';

	// Subtle content settle-in. Runs once when the shell MOUNTS — i.e. on a fresh
	// document load, which is every cross-zone microfrontend landing — so the page gently
	// rises + fades in instead of snapping after the hard nav. Only the content area
	// animates; the sidebar/shell stay put. In-app soft navs don't remount the shell, so
	// this does NOT replay on every click (kept subtle, not annoying); those keep each
	// app's onNavigate view transition. SSR-safe: the wrapper starts at opacity:0 (CSS
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

	// The shared application shell: a zone-scoped sidebar (the CURRENT zone's routes, from `zoneNav`)
	// + a content inset headed by TWO rows — the estate navbar (sidebar trigger, project switcher,
	// cross-zone zone links, identity/theme) and, under it, a slim breadcrumb bar. They used to share
	// one row, where the breadcrumb and the zone links fought for the same horizontal space and
	// overlapped on a narrow viewport; giving each its own row is what makes the header hold up at
	// any width. Every microfrontend wraps its routes in this so they share identical chrome (no
	// drift). `pathname` comes from the consuming app's $app/state and drives the breadcrumb + active
	// nav; `me`/`meLoading` come from the zone's fetchMe() and gate the navbar's admin entries.
	let {
		pathname = '',
		project = { name: 'Default', subtitle: 'Project' },
		user = { name: 'rask', email: 'local', initials: 'RA' },
		authEnabled = false,
		zoneNav = null,
		me = null,
		meLoading = false,
		notifications = null,
		sidebarFooter,
		canvas = false,
		children,
	}: {
		pathname?: string;
		project?: Project;
		// `null` = auth-on but signed out (the navbar user menu shows "Sign in"); the default identity is the auth-off local placeholder.
		user?: NavUser | null;
		authEnabled?: boolean;
		/** The CURRENT zone's own sidebar routes; null renders an empty sidebar (pre-wiring). */
		zoneNav?: ZoneNav | null;
		/** The frozen /v1/me identity for the navbar (null = signed out / unresolved). */
		me?: Me | null;
		/** True while the zone's fetchMe() is in flight — the navbar renders skeletons. */
		meLoading?: boolean;
		/** The zone's run feed for the navbar's notification bell (see `NotificationFeed`). The shell
		 *  never fetches it; `null` renders no bell at all. */
		notifications?: NotificationFeed | null;
		/** A zone whose CONTENT owns the whole viewport — an annotation canvas, not a page of panels.
		 *  Drops the sidebar and the breadcrumb row and gives `children` the full height, while keeping the
		 *  one thing every zone must share: this header, with the project switcher and the identity cluster
		 *  in the same place at the same size.
		 *
		 *  It exists because the annotator hand-rolled its own `<header>` around a bare TopNavbar instead —
		 *  two implementations of the estate header, free to drift, and they did: that zone's account avatar
		 *  sat on the LEFT (it omitted the `min-w-0 flex-1` this file passes), and it had no project
		 *  switcher and no breadcrumb while the other three did. A missing variant is why a zone forks the
		 *  shell; adding the variant is the fix. */
		canvas?: boolean;
		/** Optional zone-owned sidebar footer (e.g. media's live service-status popover). */
		sidebarFooter?: Snippet;
		children: Snippet;
	} = $props();

	// Project-first breadcrumb via HOST: the project IS the request host, so the path
	// carries only the domain + in-project trail. The project is the breadcrumb ROOT
	// (from the host, client-side); every path segment — the domain first — is a crumb.
	// SSR has no host, so `projectName` falls back to the `project` prop until mount,
	// exactly like `project-switcher` (shared `projectFromHost` keeps them in lockstep).
	let projectSlug = $state('');
	onMount(() => {
		projectSlug = projectFromHost(window.location.host) ?? '';
	});
	const projectName = $derived(projectSlug || project.name);
	const shellProject = $derived({ name: projectName, subtitle: project.subtitle ?? 'Project' });
	const crumbs = $derived(pathCrumbs(pathname));
	// A deep path (project → domain → collection → a long resource id) does not fit a narrow row, so
	// the MIDDLE of the trail folds behind an ellipsis menu rather than squeezing every crumb into
	// illegibility — and the current page, the one crumb worth keeping, is never the thing that
	// disappears. Same breakpoint the rest of the shell folds on.
	const narrow = new IsMobile(SHELL_COLLAPSE_BREAKPOINT);
	const trail = $derived(collapseCrumbs(crumbs, narrow.current ? 2 : 4));
	// Every crumb but the last is a link back up its own path; the last IS the current page, so it
	// renders as text and carries aria-current instead.
	const lastCrumbId = $derived(trail.tail.at(-1)?.id);
</script>

<Sidebar.Provider class="h-svh overflow-hidden">
	{#if !canvas}
		<AppSidebar {pathname} {zoneNav} footer={sidebarFooter} />
	{/if}
	<Sidebar.Inset class="flex min-w-0 flex-col overflow-hidden">
		<header class="flex min-w-0 shrink-0 flex-col">
			<!-- Row 1 — the estate navbar. Integrated (sidebar-07): no border, h-14 → h-12 when the
			     sidebar is icon-collapsed. Trigger + project switcher on the left; the cross-zone
			     TopNavbar (zone links + identity/theme) takes the rest of the row on its own. -->
			<!-- `border-b` only in canvas mode: normally the breadcrumb row below carries `border-y` and
			     supplies the separation, so adding one here would double it. Canvas mode drops that row —
			     and took the divider with it, leaving the header floating against the content, which is
			     visible the moment you look at the two zones side by side. -->
			<div
				class="flex h-14 min-w-0 shrink-0 items-center gap-2 px-4 transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-12 {canvas
					? 'border-border/60 border-b'
					: ''}"
			>
				{#if !canvas}
					<!-- Nothing to toggle when there is no sidebar; the switcher and identity stay either way. -->
					<Sidebar.Trigger class="text-muted-foreground hover:text-foreground -ml-1 shrink-0" />
					<Separator orientation="vertical" class="data-[orientation=vertical]:h-4" />
				{/if}
				<ProjectSwitcher project={shellProject} />
				<Separator orientation="vertical" class="data-[orientation=vertical]:h-4" />
				<TopNavbar
					{pathname}
					{me}
					{meLoading}
					{user}
					{authEnabled}
					{notifications}
					class="min-w-0 flex-1"
				/>
			</div>
			<!-- Row 2 — the breadcrumb bar. Its own slim row, so it can never be squeezed by (or
			     overlap) the zone links; within the row, a trail too long to fit folds its MIDDLE
			     behind the ellipsis menu (`collapseCrumbs`) instead of shrinking the current page. -->
			{#if !canvas}
				<nav
					aria-label="Breadcrumb"
					class="border-border/60 bg-muted/20 flex h-9 min-w-0 shrink-0 items-center overflow-hidden border-y px-4 text-sm"
				>
					<ol class="flex min-w-0 items-center gap-1.5">
						<li class="text-muted-foreground shrink-0 capitalize">{projectName}</li>
						{#each trail.lead as crumb (crumb.id)}
							<li class="flex min-w-0 shrink-0 items-center gap-1.5">
								<ChevronRight class="text-muted-foreground/40 size-3.5 shrink-0" />
								<a
									href={crumb.href}
									class="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 truncate rounded-sm capitalize transition-colors outline-none focus-visible:ring-3"
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
												<a href={crumb.href} class="w-full truncate px-2 py-1.5 capitalize">{crumb.label}</a>
											</DropdownMenu.Item>
										{/each}
									</DropdownMenu.Content>
								</DropdownMenu.Root>
							</li>
						{/if}
						{#each trail.tail as crumb (crumb.id)}
							<li class="flex min-w-0 items-center gap-1.5">
								<ChevronRight class="text-muted-foreground/40 size-3.5 shrink-0" />
								{#if crumb.id === lastCrumbId}
									<span
										aria-current="page"
										class="text-foreground truncate font-medium capitalize"
										data-slot="breadcrumb-page">{crumb.label}</span
									>
								{:else}
									<a
										href={crumb.href}
										class="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 truncate rounded-sm capitalize transition-colors outline-none focus-visible:ring-3"
										>{crumb.label}</a
									>
								{/if}
							</li>
						{/each}
					</ol>
				</nav>
			{/if}
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
	   contentEnter fades it in (no flash). The app is a hydrated SSR app (requires JS),
	   so an initial opacity:0 is safe; GSAP sets inline opacity:1 once it runs, which
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
