<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import AppSidebar from './app-sidebar.svelte';
	import { ChevronRight } from '@lucide/svelte';
	import type { Project, NavUser } from './nav-config.js';

	// The shared application shell: ONE grouped sidebar + a content inset with an
	// integrated breadcrumb top bar. Every microfrontend wraps its routes in this so
	// they share identical chrome (no drift). `pathname` comes from the consuming
	// app's $app/state; `base` is its kit.paths.base (for breadcrumb stripping).
	let {
		pathname = '',
		base = '',
		project,
		user,
		status,
		children,
	}: {
		pathname?: string;
		base?: string;
		project?: Project;
		user?: NavUser;
		/** App-specific status snippet rendered in the footer profile dropdown (e.g. Ray health). */
		status?: Snippet;
		children: Snippet;
	} = $props();

	// Breadcrumb trail from the path (the app's base stripped, dashes → spaces).
	const crumbs = $derived(
		(base && pathname.startsWith(base) ? pathname.slice(base.length) : pathname)
			.split('/')
			.filter(Boolean)
			.map((s) => s.replace(/-/g, ' ')),
	);
</script>

<Sidebar.Provider class="h-svh overflow-hidden">
	<AppSidebar {pathname} {project} {user} {status} />
	<Sidebar.Inset class="flex min-w-0 flex-col overflow-hidden">
		<!-- Integrated top bar — no border, same bg as the content so it blends in.
		     Trigger + breadcrumb only; theme/status/profile live in the sidebar footer. -->
		<header class="flex h-12 shrink-0 items-center gap-2 px-3">
			<Sidebar.Trigger class="text-muted-foreground hover:text-foreground -ml-1" />
			<nav aria-label="Breadcrumb" class="flex min-w-0 items-center gap-1.5 text-sm">
				<span class="text-muted-foreground shrink-0">{project?.name ?? 'rask'}</span>
				{#each crumbs as crumb (crumb)}
					<ChevronRight class="text-muted-foreground/40 size-3.5 shrink-0" />
					<span class="text-foreground truncate font-medium capitalize">{crumb}</span>
				{/each}
			</nav>
		</header>
		<div class="flex min-h-0 flex-1 flex-col overflow-hidden">
			{@render children()}
		</div>
	</Sidebar.Inset>
</Sidebar.Provider>
