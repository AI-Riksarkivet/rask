<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import ProjectSwitcher from './project-switcher.svelte';
	import PageNav from './page-nav.svelte';
	import type { MeProject, Project, PageNav as PageNavConfig } from './nav-config.js';

	// The app-scoped sidebar: collapsible-to-icon, carrying ONLY the CURRENT page's own routes
	// (from the `pageNav` prop each page passes). Everything estate-wide moved up into the shell
	// header — the full-page list and identity/theme to the navbar row, the project switcher to
	// the head of that same row — so the sidebar is in-app navigation only.
	let {
		pathname = '',
		pageNav = null,
		project,
		projects = [],
		content,
	}: {
		pathname?: string;
		pageNav?: PageNavConfig | null;
		project?: Project;
		/** The caller's project memberships (/management/v1/me) — the switcher's dropdown list. */
		projects?: MeProject[];
		/** Page-owned rail content rendered UNDER the app nav — e.g. the workbench's saved-views
		 *  list. A snippet rather than a nav shape because such content is page chrome (it changes
		 *  as the user works), which `PageNav`'s static route table cannot express. */
		content?: Snippet;
	} = $props();
</script>

<Sidebar.Root collapsible="icon">
	<!-- The header IS the project switcher: which project you are in, the projects you can move to, and
	     the way back to the main menu — one control, in the slot every product puts project context.
	     The page's name is not printed here: the top-navbar highlight and the breadcrumb already say it
	     twice, and a third copy would spend the rail's most valuable slot on information the user
	     already has. Nor is a Home row: the dropdown's own "Main menu" item already goes there. -->
	<Sidebar.Header>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
				<ProjectSwitcher {project} {projects} />
			</Sidebar.MenuItem>
		</Sidebar.Menu>
	</Sidebar.Header>
	<Sidebar.Content class="pt-2">
		<PageNav {pathname} nav={pageNav} />
		{#if content}{@render content()}{/if}
	</Sidebar.Content>
	{#if pageNav?.footer}
		<!-- PINNED, not scrolled. `pageNav.footer` is real nav (the dock lives here), rendered by the
		     SAME `PageNav` component as the rail via a synthetic one-group nav — so a footer leaf keeps
		     active-state matching, the reload flag and its collapsed-rail tooltip, with no second
		     renderer to drift. -->
		<Sidebar.Footer>
			<PageNav {pathname} nav={{ title: pageNav.title, groups: [pageNav.footer] }} />
		</Sidebar.Footer>
	{/if}
	<Sidebar.Rail />
</Sidebar.Root>
