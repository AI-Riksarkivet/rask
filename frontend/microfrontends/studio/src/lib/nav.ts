import { Workflow } from '@lucide/svelte';
import { exact, type ZoneNav } from '@rask/ui/shell';

// The studio zone's OWN sidebar — ONE row, because the zone is one surface: the flow canvas, which
// is the zone ROOT. It listed `Apps` (a launcher grid whose only card opened the builder) and
// `Animation` (a GSAP-vs-Svelte A/B toy) beside it; both are deleted, routes and all. A landing that
// is a menu of one real app spends the zone's best surface on a redirect, and the A/B demo had
// answered its question long ago.
//
// The rail's substance is no longer this list at all — it is the NODE LIBRARY the page injects
// through the shell's `sidebarContent` seam (see lib/flows/NodeLibrary.svelte). That is also why one
// row does not hide the rail: the shell's `hasNav` is `leaves > 1 || sidebarContent !== undefined`,
// and `@rask/zone-contract`'s leaf-count gate now reads that same condition rather than the leaf
// count alone.
export const STUDIO_ZONE_NAV: ZoneNav = {
	title: 'Studio',
	root: { title: 'Flow canvas', href: '/studio', match: exact('/studio'), icon: Workflow },
	groups: [],
};
