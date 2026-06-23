<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import { Separator } from '../components/separator/index.js';
	import AppSidebar from './app-sidebar.svelte';
	import { ChevronRight } from '@lucide/svelte';
	import type { Project, NavUser } from './nav-config.js';

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
	const crumbs = $derived(segs.slice(1).map((s) => s.replace(/-/g, ' ')));
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
					{#each crumbs as crumb (crumb)}
						<ChevronRight class="text-muted-foreground/40 size-3.5 shrink-0" />
						<span class="text-foreground truncate font-medium capitalize">{crumb}</span>
					{/each}
				</nav>
			</div>
		</header>
		<div class="flex min-h-0 flex-1 flex-col overflow-hidden">
			{@render children()}
		</div>
	</Sidebar.Inset>
</Sidebar.Provider>
