<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import { Separator } from '../components/separator/index.js';
	import AppSidebar from './app-sidebar.svelte';
	import { ChevronRight } from '@lucide/svelte';
	import { gsap } from 'gsap';
	import type { Project, NavUser } from './nav-config.js';

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

	// Project-first breadcrumb: the path is /<project>/<domain>/… so the FIRST segment
	// is the project (the breadcrumb root), and the rest is the in-project trail.
	const segs = $derived(pathname.split('/').filter(Boolean));
	const projectName = $derived(segs[0] ?? project.name);
	// The sidebar's project always reflects the URL's project segment (single source of
	// truth), so it can't drift to the prop default when an app forgets to pass `project`.
	const sidebarProject = $derived({ name: projectName, subtitle: project.subtitle ?? 'Project' });
	// Key by the accumulated path prefix so repeated segments (e.g. /studio/studio) stay unique;
	// the human label drops the dashes.
	const crumbs = $derived(
		segs.slice(1).map((seg, i) => ({
			id: segs.slice(0, i + 2).join('/'),
			label: seg.replace(/-/g, ' '),
		})),
	);
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
