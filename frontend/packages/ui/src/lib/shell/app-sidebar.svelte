<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import ZoneNav from './zone-nav.svelte';
	import type { ZoneNav as ZoneNavConfig } from './nav-config.js';

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
	<Sidebar.Content class="pt-2">
		<ZoneNav {pathname} nav={zoneNav} />
	</Sidebar.Content>
	{#if footer}
		<Sidebar.Footer>{@render footer()}</Sidebar.Footer>
	{/if}
	<Sidebar.Rail />
</Sidebar.Root>
