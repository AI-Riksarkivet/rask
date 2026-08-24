import {
	CircuitBoard,
	Database,
	Boxes,
	FileText,
	Gauge,
	Import,
	LayoutDashboard,
	ListTree,
	Route,
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
					// The run board. It was REMOVED for a while, because this row pointed at a route
					// that did not exist and `nav-truth.test.ts` ("every sidebar href resolves to a
					// real route") caught it — a row linking to a 404 advertises a capability the
					// estate does not have.
					//
					// It is back because the page is back, and the page reads LINEAGE rather than the
					// ingest service: that service still cannot list its own runs (three routes; a
					// `RunStore` with only get/put, backed by a per-pod dict that is deliberately not
					// durable). The graph every run writes to at START and COMPLETE/FAIL is the
					// durable record of which runs exist.
					//
					// `seg` not `exact`: it lights up for `/compute/ingest/<run_id>` too, which is the
					// detail page this list links into.
					title: 'Ingest runs',
					href: '/compute/ingest',
					match: seg('/compute/ingest'),
					icon: ListTree,
				},
			],
		},
		{
			label: 'Workloads',
			items: [
				{ title: 'Batch jobs', href: '/compute/jobs', match: seg('/compute/jobs'), icon: ListTree },
				// A transform declares WHAT a job runs — its Ray entrypoint and params. It sits beside Jobs
				// because that is where a person watching a run goes to change what the run does; a
				// declaration in one zone and its execution in another was the split that made this
				// unusable. The RECORD still lives in the catalog, admin-gated and audited — only the
				// caller moved (overturns 21b17f1a's "compute observes Ray, it does not drive it").
				{ title: 'Transforms', href: '/compute/transforms', match: seg('/compute/transforms'), icon: Route },
				{ title: 'Serve', href: '/compute/serve', match: seg('/compute/serve'), icon: ServerCog },
				// NO 'Inference' ROW. #131 moved an inference PLAYGROUND here from /models/playground on
				// the reasoning that running a deployment is a compute verb — which was right about the
				// verb and wrong about needing a second surface. Deleted 2026-08-07: it could only ever
				// call ONE Serve app (a single `COMPUTE_SERVE_URL`, no per-request app or path) and its
				// UI was HTR end to end — an image picker up, an ALTO parser down — so it could not test
				// any of the estate's other live deployments. Studio's flow canvas is the general form of
				// the same idea (`?app=&path=` + a typed payload per node, against the EXTERNAL Ray
				// cluster where the GPU apps actually run), so this was a narrower duplicate of a surface
				// that already worked.
				//
				// Deliberately NOT replaced by a cross-zone leaf into /studio. The estate's ruling is
				// that the TOP NAVBAR owns cross-zone hops — it is the one surface that applies
				// `data-sveltekit-reload` itself — and `nav-truth.test.ts` pins the cross-zone sidebar
				// set as EMPTY. Studio already has a navbar entry in every zone, this one included.
			],
		},
		{
			// I/O — data going IN to the estate and questions coming OUT of it. Ingest sat under
			// "Workloads" beside Jobs and Serve, which names how it RUNS rather than what it is for;
			// grouped with the query engine it reads as the estate's data doorway.
			label: 'I/O',
			items: [
				// NO ingest row here. The form lives at `/compute/etl` and is already the "ETL" row of
				// the Ingest group above; the copy that used to sit here pointed at `/compute/new` — a
				// route deleted in the same change that created `/compute/etl` — and named an icon
				// (`Upload`) that was never imported, so evaluating this module threw
				// `ReferenceError: Upload is not defined` and took the whole zone's layout down: every
				// compute page 500'd, not just the dead link.
				// SCAFFOLD (2026-08-06): no query backend exists yet. It ships as a nav leaf + a
				// scaffold-badged page ON PURPOSE — R15 is law, a zone surface missing from the rail is
				// a defect regardless of scaffold status, and a visible stub is how the shape gets
				// reviewed before the engine is built.
				{
					title: 'Query engine',
					href: '/compute/query',
					match: seg('/compute/query'),
					icon: Database,
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
