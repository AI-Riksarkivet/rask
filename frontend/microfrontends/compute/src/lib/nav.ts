import {
	Boxes,
	FileText,
	Gauge,
	Import,
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
			],
		},
		{
			// INGEST is its own group, not a row of Workloads. Workloads answers "what is running on
			// the Ray cluster"; ingest answers "how does data get INTO the estate" — it never touches
			// Ray, it drives the ingest plane's control API. Filing it under Workloads is what made it
			// read as a Ray job, and the run view alongside it is the ETL run, not a Ray job.
			label: 'Ingest',
			items: [
				{
					// ETL — the estate's name for this plane, so the ROW says it and not a verb. The
					// route was `/compute/new` ("new" names the action, not the thing) and sat in no
					// sidebar group at all, reachable only by typing the URL; renaming the route to
					// `/compute/etl` then labelling the row "New run" just moved the same mistake up a
					// layer. The row is the noun; "Runs" beside it is the same noun's history.
					title: 'ETL',
					href: '/compute/etl',
					match: seg('/compute/etl'),
					icon: Import,
				},
				{
					// The run view. `seg` not `exact`: this lights up for /compute/ingest/<run_id>,
					// which is the only way the page is ever reached.
					title: 'Runs',
					href: '/compute/ingest',
					match: seg('/compute/ingest'),
					icon: ListTree,
				},
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
