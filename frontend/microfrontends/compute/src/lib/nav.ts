import {
	CircuitBoard,
	ScanText,
	Upload,
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
	// Overview is the ZONE ROOT, not a Cluster row. It summarises all three groups — jobs and serve
	// as much as node health — so filing it under "Cluster" read as "cluster overview" and misnamed
	// the one page that covers everything. `root` renders it above the groups, ungrouped.
	root: { title: 'Overview', href: '/compute', match: exact('/compute'), icon: Gauge },
	groups: [
		{
			label: 'Cluster',
			items: [
				{ title: 'Nodes', href: '/compute/cluster', match: seg('/compute/cluster'), icon: Server },
				{ title: 'Actors', href: '/compute/actors', match: seg('/compute/actors'), icon: Boxes },
				// DCGM (2026-08-05): the GPU metrics surface — a dummy until the exporter is wired.
				{ title: 'GPU', href: '/compute/gpu', match: seg('/compute/gpu'), icon: CircuitBoard },
			],
		},
		{
			label: 'Workloads',
			items: [
				{ title: 'Jobs', href: '/compute/jobs', match: seg('/compute/jobs'), icon: ListTree },
				{ title: 'Serve', href: '/compute/serve', match: seg('/compute/serve'), icon: ServerCog },
				// The ingest form has existed since the zone did and was in NO nav — reachable only by
				// typing the URL, and the route was renamed `etl` -> `new` underneath anyone who had.
				// A page that starts a real pipeline run must be reachable from the rail.
				{ title: 'Ingest', href: '/compute/new', match: seg('/compute/new'), icon: Upload },
				// #131: inference is a COMPUTE concern, not a model-registry one. Moved here from
				// /models/playground by ruling — you send an image to a live Serve deployment and read
				// what comes back, which is the same plane as Jobs and Serve, not the registry.
				{
					title: 'Inference',
					href: '/compute/inference',
					match: seg('/compute/inference'),
					icon: ScanText,
				},
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
	// PINNED to the rail's bottom — the dock composes the whole zone, so it sits below the areas.
	footer: {
		label: 'Workspace',
		items: [
			{
				title: 'Workbench',
				href: '/compute/workbench',
				match: seg('/compute/workbench'),
				icon: LayoutDashboard,
			},
		],
	},
};
