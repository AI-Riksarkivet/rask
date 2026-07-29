<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import ProjectSwitcher from './project-switcher.svelte';
	import ZoneNav from './zone-nav.svelte';
	import type { Project, ZoneNav as ZoneNavConfig } from './nav-config.js';

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
	<!-- The header IS the project switcher: which project you are in, the projects you can move to, and
	     the way back to the main menu — one control, in the slot every product puts project context.
	     It used to print the ZONE's name, which the top-navbar highlight and the breadcrumb already say
	     twice; a third copy spent the rail's most valuable slot on information the user already had.
	     A separate Home row sat under it for a while, which was the same mistake in miniature — the
	     dropdown's own "Main menu" item already goes there, so the row was a second door to one room. -->
	<Sidebar.Header>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
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
