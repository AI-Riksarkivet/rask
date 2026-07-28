<script lang="ts">
	import type { Snippet } from 'svelte';
	import { House } from '@lucide/svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import ZoneNav from './zone-nav.svelte';
	import { prefetchOnIntent, type ZoneNav as ZoneNavConfig } from './nav-config.js';

	// The zone-scoped sidebar: collapsible-to-icon, carrying ONLY the CURRENT zone's own routes
	// (from the `zoneNav` prop each zone passes). Everything estate-wide moved up into the shell
	// header — the cross-zone list and identity/theme to the navbar row, the project switcher to
	// the head of that same row — so the sidebar is in-zone navigation only, plus an OPTIONAL
	// zone-owned `footer` snippet (e.g. media's live service-status popover).
	let {
		pathname = '',
		zoneNav = null,
		footer,
	}: {
		pathname?: string;
		zoneNav?: ZoneNavConfig | null;
		footer?: Snippet;
	} = $props();
</script>

<Sidebar.Root collapsible="icon">
	<!-- The header names the zone you are in AND is the way back to the estate root. It had no header
	     at all, so the only route home was hunting for "Home" among the cross-zone links in the
	     navbar — every other product puts it exactly here, at the top of the rail, and users reach
	     for it. Cross-zone by definition (home owns '/'), so it hard-navigates and warms the target
	     on intent, like every other cross-zone link in the shell. Collapsed to icon it stays a
	     house, which is the one glyph nobody has to learn. -->
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
								<span class="truncate font-medium">{zoneNav?.title ?? 'rask'}</span>
								<span class="text-muted-foreground truncate text-xs">Back to home</span>
							</div>
						</a>
					{/snippet}
				</Sidebar.MenuButton>
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
