import {
	Boxes,
	FileText,
	Gauge,
	LayoutDashboard,
	ListTree,
	ScrollText,
	Server,
	ServerCog,
} from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The compute zone's OWN sidebar routes (the shared shell renders exactly what a zone passes — the
// cross-zone list lives in the top navbar). Hrefs are absolute domain paths: the zone is served
// under its `/compute` base both standalone (dev/e2e) and behind the ingress. Overview is the
// landing — the batches/HTR dashboard folded in from the retired overview zone (R16) — and sits at
// the zone root, so it matches EXACTLY (`seg` would light it up on every sibling). Every leaf is
// same-zone, so they all stay soft navs (no `reload`).
//
// Grouped by the question each row answers: what is the cluster doing, what work is on it, and how
// do I debug it. A flat list of seven made "Logs" and "Cluster" look like peers.
export const COMPUTE_ZONE_NAV: ZoneNav = {
	title: 'Compute',
	groups: [
		{
			label: 'Cluster',
			items: [
				{ title: 'Overview', href: '/compute', match: exact('/compute'), icon: Gauge },
				{
					// Jobs + capacity + actors in one dock, so "is this job starved or stuck?" is a glance
					// rather than two navigations.
					title: 'Workbench',
					href: '/workbench/',
					match: seg('/workbench'),
					reload: true,
					icon: LayoutDashboard,
				},
				{ title: 'Nodes', href: '/compute/cluster', match: seg('/compute/cluster'), icon: Server },
				{ title: 'Actors', href: '/compute/actors', match: seg('/compute/actors'), icon: Boxes },
			],
		},
		{
			label: 'Workloads',
			items: [
				{ title: 'Jobs', href: '/compute/jobs', match: seg('/compute/jobs'), icon: ListTree },
				{ title: 'Serve', href: '/compute/serve', match: seg('/compute/serve'), icon: ServerCog },
			],
		},
		{
			label: 'Diagnostics',
			items: [
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
		},
	],
};
