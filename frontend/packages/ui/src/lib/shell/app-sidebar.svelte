<script lang="ts">
	import * as Sidebar from '../components/sidebar/index.js';
	import PageNav from './page-nav.svelte';
	import type { PageNav as PageNavConfig } from './nav-config.js';

	// The console's one navigation: every page's routes in one collapsible-to-icon rail. The top bar
	// carries only the rail's toggle, the breadcrumb, the bell and the account menu.
	let { pathname = '', pageNav = null }: { pathname?: string; pageNav?: PageNavConfig | null } =
		$props();
</script>

<Sidebar.Root collapsible="icon">
	<!-- One landmark over the rail and its footer, so a screen reader and a test find the console's
	     navigation by name. `contents` keeps both as items of the sidebar's own flex column. -->
	<nav aria-label="Pages" class="contents">
		<Sidebar.Content class="pt-2">
			<PageNav {pathname} nav={pageNav} />
		</Sidebar.Content>
		{#if pageNav?.footer}
			<!-- PINNED, not scrolled: rendered by the same `PageNav` as the rail through a one-group nav,
			     so a footer leaf keeps active-state matching and its collapsed-rail tooltip. The group
			     inside carries the padding, so the footer adds none and its rows line up with the rail's. -->
			<Sidebar.Footer class="p-0">
				<PageNav {pathname} nav={{ groups: [pageNav.footer] }} />
			</Sidebar.Footer>
		{/if}
	</nav>
	<Sidebar.Rail />
</Sidebar.Root>
