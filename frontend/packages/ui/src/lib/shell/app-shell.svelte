<script lang="ts">
	import { onMount, type Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import { Separator } from '../components/separator/index.js';
	import AppSidebar from './app-sidebar.svelte';
	import { ChevronRight } from '@lucide/svelte';
	import { gsap } from 'gsap';
	import type { Project, NavUser } from './nav-config.js';
	import { pathCrumbs, projectFromHost } from './breadcrumb.js';

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

	// The shared application shell: ONE grouped sidebar + a content inset with an
	// integrated breadcrumb top bar. Every microfrontend wraps its routes in this so
	// they share identical chrome (no drift). `pathname` comes from the consuming
	// app's $app/state and drives the breadcrumb + active nav.
	let {
		pathname = '',
		project = { name: 'Default', subtitle: 'Project' },
		user = { name: 'rask', email: 'local', initials: 'RA' },
		children,
	}: {
		pathname?: string;
		project?: Project;
		user?: NavUser;
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
	const sidebarProject = $derived({ name: projectName, subtitle: project.subtitle ?? 'Project' });
	const crumbs = $derived(pathCrumbs(pathname));
</script>

<Sidebar.Provider class="h-svh overflow-hidden">
	<AppSidebar {pathname} project={sidebarProject} {user} />
	<Sidebar.Inset class="flex min-w-0 flex-col overflow-hidden">
		<!-- Integrated top bar (sidebar-07): no border, h-16 → h-12 when the sidebar is
		     icon-collapsed. Trigger + breadcrumb; theme/profile live in the footer. -->
		<header
			class="flex h-16 shrink-0 items-center gap-2 transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-12"
		>
			<div class="flex items-center gap-2 px-4">
				<Sidebar.Trigger class="text-muted-foreground hover:text-foreground -ml-1" />
				<Separator orientation="vertical" class="mr-2 data-[orientation=vertical]:h-4" />
				<nav aria-label="Breadcrumb" class="flex min-w-0 items-center gap-1.5 text-sm">
					<span class="text-muted-foreground shrink-0 capitalize">{projectName}</span>
					{#each crumbs as crumb (crumb.id)}
						<ChevronRight class="text-muted-foreground/40 size-3.5 shrink-0" />
						<span class="text-foreground truncate font-medium capitalize">{crumb.label}</span>
					{/each}
				</nav>
			</div>
		</header>
		<div class="content-enter flex min-h-0 flex-1 flex-col overflow-hidden" {@attach contentEnter}>
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
