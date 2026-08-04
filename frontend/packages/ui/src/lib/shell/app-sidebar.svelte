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
		content,
	}: {
		pathname?: string;
		zoneNav?: ZoneNavConfig | null;
		project?: Project;
		footer?: Snippet;
		/** Zone-owned rail content rendered UNDER the zone nav — e.g. the workbench's saved-views
		 *  list. A snippet rather than a nav shape because such content is page chrome (it changes
		 *  as the user works), which `ZoneNav`'s static route table cannot express. */
		content?: Snippet;
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
		{#if content}{@render content()}{/if}
	</Sidebar.Content>
	{#if zoneNav?.footer || footer}
		<!-- PINNED, not scrolled. `zoneNav.footer` is real nav (the dock lives here), rendered by the
		     SAME `ZoneNav` component as the rail via a synthetic one-group nav — so a footer leaf keeps
		     active-state matching, the reload flag and its collapsed-rail tooltip, with no second
		     renderer to drift. The free-form `footer` snippet stays for page chrome that is not
		     navigation at all (the explorer's live service-status badge). -->
		<Sidebar.Footer>
			{#if zoneNav?.footer}
				<ZoneNav {pathname} nav={{ title: zoneNav.title, groups: [zoneNav.footer] }} />
			{/if}
			{#if footer}{@render footer()}{/if}
		</Sidebar.Footer>
	{/if}
	<Sidebar.Rail />
</Sidebar.Root>
