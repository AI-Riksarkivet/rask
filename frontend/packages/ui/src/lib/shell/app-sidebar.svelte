<script lang="ts">
	import type { Snippet } from 'svelte';
	import { House } from '@lucide/svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import ProjectSwitcher from './project-switcher.svelte';
	import ZoneNav from './zone-nav.svelte';
	import { prefetchOnIntent, type Project, type ZoneNav as ZoneNavConfig } from './nav-config.js';

	// The zone-scoped sidebar: collapsible-to-icon, carrying ONLY the CURRENT zone's own routes
	// (from the `zoneNav` prop each zone passes). Everything estate-wide moved up into the shell
	// header — the cross-zone list and identity/theme to the navbar row, the project switcher to
	// the head of that same row — so the sidebar is in-zone navigation only, plus an OPTIONAL
	// zone-owned `footer` snippet (e.g. media's live service-status popover).
	let {
		pathname = '',
		zoneNav = null,
		project,
		footer,
	}: {
		pathname?: string;
		zoneNav?: ZoneNavConfig | null;
		project?: Project;
		footer?: Snippet;
	} = $props();
</script>

<Sidebar.Root collapsible="icon">
	<!-- The header carries the PROJECT switcher and the way home. It used to print the zone's own name,
	     which said nothing: the active zone is already stated twice above — highlighted in the top
	     navbar and spelled out in the breadcrumb — so a third copy spent the most valuable slot in the
	     rail on information the user already had. What was missing there is the thing you cannot get
	     anywhere else: which project you are in, and how to leave it.
	     The house stays as the estate-root link (cross-zone, so it hard-navigates and warms on intent),
	     and collapsed to icon that glyph is what remains — nobody has to learn it. -->
	<Sidebar.Header>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
				<Sidebar.MenuButton size="lg" tooltipContent="Home">
					{#snippet child({ props })}
						<a href="/" data-sveltekit-reload {...props} {@attach prefetchOnIntent('/')}>
							<div
								class="bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 items-center justify-center rounded-lg"
							>
								<House class="size-4" />
							</div>
							<div class="grid flex-1 text-left text-sm leading-tight">
								<span class="truncate font-medium">Home</span>
								<span class="text-muted-foreground truncate text-xs">Back to the estate root</span>
							</div>
						</a>
					{/snippet}
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
			<!-- Hidden when the rail is icon-collapsed: a dropdown trigger squeezed into a 3rem rail is a
			     mystery button, and the switcher is a deliberate act rather than something reached for
			     mid-scan. -->
			<Sidebar.MenuItem class="group-data-[collapsible=icon]:hidden">
				<ProjectSwitcher {project} />
			</Sidebar.MenuItem>
		</Sidebar.Menu>
	</Sidebar.Header>
	<Sidebar.Content class="pt-2">
		<ZoneNav {pathname} nav={zoneNav} />
	</Sidebar.Content>
	{#if footer}
		<Sidebar.Footer>{@render footer()}</Sidebar.Footer>
	{/if}
	<Sidebar.Rail />
</Sidebar.Root>
