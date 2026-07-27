import { Boxes, FileText, Gauge, ListTree, ScrollText, Server, ServerCog } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The compute zone's OWN sidebar routes (the shared shell renders exactly what a zone passes — the
// cross-zone list lives in the top navbar). Hrefs are absolute domain paths: the zone is served
// under its `/compute` base both standalone (dev/e2e) and behind the ingress. Overview is the
// landing — the batches/HTR dashboard folded in from the retired overview zone (R16) — and sits at
// the zone root, so it matches EXACTLY (`seg` would light it up on every sibling). Every leaf is
// same-zone, so they all stay soft navs (no `reload`).
export const COMPUTE_ZONE_NAV: ZoneNav = {
	title: 'Compute',
	leaves: [
		{ title: 'Overview', href: '/compute', match: exact('/compute'), icon: Gauge },
		{ title: 'Jobs', href: '/compute/jobs', match: seg('/compute/jobs'), icon: ListTree },
		{ title: 'Actors', href: '/compute/actors', match: seg('/compute/actors'), icon: Boxes },
		{ title: 'Cluster', href: '/compute/cluster', match: seg('/compute/cluster'), icon: Server },
		{ title: 'Serve', href: '/compute/serve', match: seg('/compute/serve'), icon: ServerCog },
		{
			title: 'Logs',
			href: '/compute/logviewer',
			match: seg('/compute/logviewer'),
			icon: ScrollText,
		},
		{
			title: 'API docs',
			href: '/compute/api-docs',
			match: seg('/compute/api-docs'),
			icon: FileText,
		},
	],
};
