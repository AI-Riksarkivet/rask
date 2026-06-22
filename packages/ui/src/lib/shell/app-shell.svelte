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
	// app's $app/state; `base` is its kit.paths.base (for breadcrumb stripping).
	let {
		pathname = '',
		project = { name: 'Default', subtitle: 'Project' },
		user = { name: 'rask', email: 'local', initials: 'RA' },
		status,
		children,
	}: {
		pathname?: string;
		project?: Project;
		user?: NavUser;
		/** App-specific status snippet rendered in the footer profile dropdown (e.g. Ray health). */
		status?: Snippet;
		children: Snippet;
	} = $props();

	// Breadcrumb trail from the FULL path (incl. the base segment, e.g. /compute) so
	// pressing "Compute" reads "Compute › Overview", not just "Overview".
	const crumbs = $derived(
		pathname
			.split('/')
			.filter(Boolean)
			.map((s) => s.replace(/-/g, ' ')),
	);
</script>

<Sidebar.Provider class="h-svh overflow-hidden">
	<AppSidebar {pathname} {project} {user} {status} />
	<Sidebar.Inset class="flex min-w-0 flex-col overflow-hidden">
		<!-- Integrated top bar (sidebar-07): no border, h-16 → h-12 when the sidebar is
		     icon-collapsed. Trigger + breadcrumb; theme/status/profile live in the footer. -->
		<header
			class="flex h-16 shrink-0 items-center gap-2 transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-12"
		>
			<div class="flex items-center gap-2 px-4">
				<Sidebar.Trigger class="text-muted-foreground hover:text-foreground -ml-1" />
				<Separator orientation="vertical" class="mr-2 data-[orientation=vertical]:h-4" />
				<nav aria-label="Breadcrumb" class="flex min-w-0 items-center gap-1.5 text-sm">
					<span class="text-muted-foreground shrink-0">{project.name}</span>
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
