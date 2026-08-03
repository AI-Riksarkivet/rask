import {
	Brain,
	Cpu,
	Database,
	FlaskConical,
	LayoutDashboard,
	PenLine,
	Search,
} from '@lucide/svelte';
import type { RunStatusLike } from '../runs/run-status.js';

/** All lucide icons share one component signature, so any icon's type fits. */
export type IconComponent = typeof Database;

/** What a zone hands the shell to light up the navbar's notification bell.
 *
 *  One optional object rather than six threaded props, so a zone opts in with a single `notifications=`
 *  and a zone that has not wired the feed yet renders no bell at all (rather than an empty one that
 *  looks broken). The shell never fetches it: `runs` is the zone's own `GET /runs` read, and the read
 *  state comes back through the callbacks so the zone — which owns a per-subject store — can persist
 *  it. Ids in `seen`/`dismissed` are NOTIFICATION ids (`run_id@STATE`), from `runNotificationId`. */
export type NotificationFeed = {
	/** The run rows, as the lineage service's `GET /runs` returns them. */
	runs: RunStatusLike[];
	seen?: string[];
	dismissed?: string[];
	onseen?: (seen: string[]) => void;
	ondismiss?: (notificationId: string, dismissed: string[]) => void;
	/** Optional "see everything" destination — the zone's own runs page. */
	allHref?: string;
};

/** A leaf route inside the CURRENT zone's sidebar — same-zone (soft nav) unless `reload` says
 *  otherwise. */
export type ZoneNavLeaf = {
	title: string;
	/** ABSOLUTE, domain-relative href (e.g. /lakehouse/catalog/tables). */
	href: string;
	/** Active predicate vs the FULL pathname. */
	match: (p: string) => boolean;
	icon?: IconComponent;
	/** True for a leaf that leaves this zone's route manifest (e.g. media's Annotate → /annotator):
	 *  the sidebar link then hard-navigates (data-sveltekit-reload) instead of soft-routing. */
	reload?: boolean;
	/** Sub-routes, rendered as a `Sidebar.MenuSub` under this leaf and auto-expanded while any of
	 *  them (or the parent) is active. One level only — a third tier is a sign the ZONE should have
	 *  been split, not the menu. */
	children?: ZoneNavLeaf[];
};

/** One labelled section of the sidebar — a `Sidebar.Group` with its own `GroupLabel`. */
export type ZoneNavGroup = {
	/** The section heading (e.g. "Catalog", "Lineage", "Governance"). */
	label: string;
	items: ZoneNavLeaf[];
	/** Collapsible via the label (default true). Set false to pin a section open. */
	collapsible?: boolean;
	/** Start collapsed. Ignored while the group contains the active route — a section that hides
	 *  where you currently are reads as a broken sidebar. */
	defaultCollapsed?: boolean;
};

/** The zone ROOT row — rendered above every group, with no group label of its own.
 *
 *  A zone root is not a member of any thematic section: it is the landing that SUMMARISES all of
 *  them. Compute's Overview had to sit inside "Cluster" purely because `groups[]` was the only
 *  container on offer, which reads as "cluster overview" when it actually covers jobs and serve
 *  too. Optional on purpose — a zone whose root genuinely belongs to a section (media's Search
 *  really is Explore) should keep it there rather than promote it. */
export type ZoneNavRoot = ZoneNavLeaf;

/** The per-zone sidebar config: each zone passes ITS OWN routes to the shared AppShell. The zone
 *  list itself lives in the top navbar (`topNav`) — the sidebar never renders other zones.
 *
 *  GROUPED, not flat. This was `{ title, leaves[] }` — one unlabelled list — which forced Lakehouse
 *  to swap the WHOLE sidebar per area, keyed off the pathname, so standing in the catalog you
 *  could not see lineage, models, governance or admin at all. Storage was reachable only by typing
 *  its URL, because no area's flat list could mention another area's route. Groups let one zone
 *  present its whole surface at once, which is what the shadcn sidebar primitives were built for
 *  and what the estate already had installed but never used. */
export type ZoneNav = {
	/** The zone's display name — shown as the sidebar's own heading. */
	title: string;
	/** The landing row, above and outside every group. See {@link ZoneNavRoot}. */
	root?: ZoneNavRoot;
	groups: ZoneNavGroup[];
};

/** Every leaf in a zone's nav, flattened depth-first (parents before their children). The shell uses
 *  it to decide whether a rail is worth rendering; `@rask/zone-contract` uses it to walk every href. */
export function zoneNavLeaves(nav: ZoneNav | null | undefined): ZoneNavLeaf[] {
	if (!nav) return [];
	const out: ZoneNavLeaf[] = [];
	// The root FIRST, and it must be here: zone-contract walks this list to gate every href
	// (deploy paths, cross-zone reload, trailing slashes). A root that skipped it would be the one
	// link in the zone nobody checks.
	if (nav.root) {
		out.push(nav.root);
		if (nav.root.children) out.push(...nav.root.children);
	}
	for (const group of nav.groups) {
		for (const item of group.items) {
			out.push(item);
			if (item.children) out.push(...item.children);
		}
	}
	return out;
}

/** A selectable project — the sidebar header switcher. One implicit "default" today. */
export type Project = { name: string; subtitle?: string };

/** The navbar profile identity — populated from the OIDC session (per-zone +layout). */
export type NavUser = { name: string; email?: string; initials?: string };

/** One project membership row from `/v1/me` — the tenant and the caller's role in it. */
export type MeProject = { project: string; role: 'admin' | 'member' };

/** The frozen `GET /v1/me` identity contract, mirrored structurally from @rask/api (the shared shell
 *  never imports app data — same seam as `NavUser`): any verified caller's sub/name/email, whether
 *  they hold the estate-admin privilege, and their project memberships. */
export type Me = {
	sub: string;
	name: string | null;
	email: string | null;
	estate_admin: boolean;
	projects: MeProject[];
};

/** Drop a single trailing slash (except on root "/"). A zone served under a base path reports its ROOT
 *  as `page.url.pathname === '/models/'` (trailing slash), which would fail an exact compare against
 *  '/models'; normalizing here makes every matcher trailing-slash-robust. */
export const norm = (p: string) => (p.length > 1 && p.endsWith('/') ? p.slice(0, -1) : p);
/** prefix-segment match: active when the path equals the href or is nested under it. */
export const seg = (href: string) => (p: string) =>
	norm(p) === href || norm(p).startsWith(href + '/');
/** exact match: active ONLY on this exact path. Used for a root leaf whose href equals its own
 *  zone's href (e.g. Registry=/models, Graph=/lineage) — `seg` there would over-match every sibling
 *  sub-route (/models/pipeline would light up Registry too), so those leaves match exactly. */
export const exact = (href: string) => (p: string) => norm(p) === href;
/** domain match: active when the path is under any of the given prefixes. */
export const under =
	(...prefixes: string[]) =>
	(p: string) =>
		prefixes.some((pre) => norm(p) === pre || norm(p).startsWith(pre + '/'));

/** One sub-area inside a zone's navbar panel — a first-class view of that zone, with a one-line
 *  description so the panel explains the estate instead of just listing words. */
export type TopNavItem = { title: string; href: string; description: string };

/** A labelled column inside a navbar panel. A trigger that gathers SEVERAL concerns (Lakehouse:
 *  catalog + models + governance) needs its rows grouped under headings, or the panel is just a
 *  long undifferentiated list — the multi-column NavigationMenu.Content shape. */
export type TopNavGroup = { label: string; items: TopNavItem[] };

/** One top-navbar entry — a whole microfrontend zone (cross-zone = hard nav). */
export type TopNavEntry = {
	title: string;
	href: string;
	match: (p: string) => boolean;
	/** The zone's mark in the bar (lucide) — same signature seam as `ZoneNavLeaf.icon`. */
	icon?: IconComponent;
	/** The zone's sub-areas. Present → the navbar renders a NavigationMenu trigger opening a panel
	 *  of these; absent → a plain link, because the zone has a single surface and a dropdown with
	 *  one row in it would be noise. Deliberately a SUBSET of the zone's own sidebar (`ZoneNav`):
	 *  this is the cross-zone jump list, not a mirror of in-zone navigation. */
	items?: TopNavItem[];
	/** Grouped alternative to `items` — rendered as labelled columns. Used by Lakehouse, whose panel
	 *  spans the catalog, the model registry and the governance surfaces. */
	groups?: TopNavGroup[];
	/** Visual weight in the bar. 'primary' is where the work happens — the lakehouse you govern and
	 *  the compute that fills it — and those lead the bar. Everything else is a real zone but a
	 *  destination you go to for a specific task, so it reads one step quieter. Six equally-weighted
	 *  entries give a newcomer no idea where to start; this is the ONLY thing that says so. */
	tier?: 'primary' | 'secondary';
};

// NO `Projects` row here, BY RULING (2026-08-03): there is ONE project concept and it is the TOP of
// the hierarchy (project > warehouse > namespace > table) — a tenants list inside a project-scoped
// zone's dropdown inverted it. The provisioning surface itself still lives at
// /lakehouse/catalog/projects (reachable from the admin area) until the IA round re-homes it to the
// top level beside the home gallery.
const DATA_ITEMS: TopNavItem[] = [
	{
		title: 'Tables',
		href: '/lakehouse/catalog/tables',
		description: 'The governed table registry.',
	},
	{
		title: 'Namespaces',
		href: '/lakehouse/catalog/namespaces',
		description: 'Medallion namespaces and their maintenance policies.',
	},
	{
		title: 'Warehouses',
		href: '/lakehouse/catalog/warehouses',
		description: 'Storage bindings — one bucket per project.',
	},
	{
		// R28: the object browser was reachable ONLY by typing the URL — the sidebar is area-scoped,
		// so no other area linked to it and the panel never listed it. Storage is an area of this
		// zone, so it belongs in this zone's panel.
		title: 'Storage',
		href: '/lakehouse/catalog/storage/',
		description: 'Objects in the warehouse buckets — browse and preview.',
	},
];

const LINEAGE_ITEMS: TopNavItem[] = [
	{
		title: 'Datasets',
		href: '/lakehouse/lineage/datasets',
		description: 'Every dataset the cascade has read or written.',
	},
	{
		title: 'Jobs',
		href: '/lakehouse/lineage/jobs',
		description: 'The compute identities that produce them.',
	},
	{
		title: 'Runs',
		href: '/lakehouse/lineage/runs',
		description: 'Individual executions, with state and errors.',
	},
	{
		title: 'Columns',
		href: '/lakehouse/lineage/columns',
		description: 'Field-level lineage across the estate.',
	},
	{
		title: 'Graph',
		href: '/lakehouse/lineage',
		description: 'The whole medallion DAG on one canvas.',
	},
];

const MEDIA_ITEMS: TopNavItem[] = [
	{ title: 'Search', href: '/media/', description: 'Semantic search over the corpus.' },
	{ title: 'Atlas', href: '/media/atlas', description: 'The embedding map of the corpus.' },
	{ title: 'Tree', href: '/media/tree', description: 'The corpus by topic hierarchy.' },
	{ title: 'Graph', href: '/media/graph', description: 'Relations between media entities.' },
	{ title: 'Workflow', href: '/media/workflow', description: 'The derivation pipeline.' },
];

const MODEL_ITEMS: TopNavItem[] = [
	{ title: 'Registry', href: '/lakehouse/models', description: 'Candidate → blessed, per model.' },
	{
		title: 'Experiments',
		href: '/lakehouse/models/experiments',
		description: 'Training runs and their metrics.',
	},
	{
		title: 'Pipeline',
		href: '/lakehouse/models/pipeline',
		description: 'Train, validate, promote.',
	},
];

/** Governance + operations over the SAME estate the catalog and registry describe — so these ride
 *  in the Lakehouse panel rather than a separate top-level Admin entry. Estate-admin only. */
const GOVERNANCE_ITEMS: TopNavItem[] = [
	{
		title: 'Access',
		href: '/lakehouse/governance/access',
		description: 'The FGA workbench: check, tuples, graph.',
	},
	{
		title: 'Tenants',
		href: '/lakehouse/admin/tenants',
		description: 'Warehouses per project, and who administers them.',
	},
	{
		title: 'Audit',
		href: '/lakehouse/governance/audit',
		description: 'The compliance trail — who did what.',
	},
];

/** COMPUTE's panel rows — the Ray/job plane. The old overview zone folded in here (R16), so the
 *  zone root IS the overview and rides the panel as its first row (matching exactly, like Media's
 *  Search at /media). */
const COMPUTE_ITEMS: TopNavItem[] = [
	{ title: 'Overview', href: '/compute/', description: 'The Ray plane at a glance.' },
	{ title: 'Jobs', href: '/compute/jobs', description: 'Submitted Ray jobs and their lifecycle.' },
	{ title: 'Cluster', href: '/compute/cluster', description: 'Nodes and their resource load.' },
	{ title: 'Actors', href: '/compute/actors', description: 'Live actors across the cluster.' },
	{ title: 'Serve', href: '/compute/serve', description: 'Ray Serve apps and deployments.' },
	{ title: 'Logs', href: '/compute/logviewer', description: 'The cluster log viewer.' },
	{
		title: 'API docs',
		href: '/compute/api-docs',
		description: 'The compute API, self-documented.',
	},
];

const OPERATIONS_ITEMS: TopNavItem[] = [
	{ title: 'Events', href: '/lakehouse/admin/events', description: 'The live control-event feed.' },
	{
		title: 'Streams',
		href: '/lakehouse/admin/streams',
		description: 'JetStream consumers and their lag.',
	},
	{
		title: 'DLQ',
		href: '/lakehouse/admin/dlq',
		description: 'Dead-lettered lineage runs, with replay.',
	},
];

/**
 * The top-navbar IA. Seven zones — home, lakehouse, media, annotator, compute, train, studio — and
 * the bar carries an entry for EVERY one of them (R15: a zone missing from the shared navbar is a
 * defect, regardless of scaffold status). Lakehouse and Lineage stay two views of the ONE merged
 * estate zone rather than two apps — a hop from the catalog to the lineage graph, or to governance,
 * is a soft navigation inside one router; every OTHER entry crosses a zone boundary and
 * hard-navigates. The sidebar renders the current zone's routes (`ZoneNav`).
 *
 * A zone with sub-areas carries `items`, and the navbar renders it as a NavigationMenu trigger with
 * a panel — so the estate's shape is reachable from any zone in one hop instead of landing on a
 * zone root and hunting through its sidebar. Zones with a single surface stay plain links.
 *
 * The Governance/Operations columns append ONLY for an estate admin (`me.estate_admin` from the
 * frozen `/v1/me` contract) — fail-closed: an unresolved/absent `me` renders the base entries.
 * Access is NOT a top-level entry: it lives inside the lakehouse admin area
 * (/lakehouse/governance/access), so it appears only as one row of the Governance column.
 */
export function topNav(estateAdmin: boolean): TopNavEntry[] {
	// LAKEHOUSE gathers everything that describes or governs the one governed estate: the catalog
	// (projects → warehouses → namespaces → tables), the model registry (models are catalog objects
	// too — models$<model> carries the same rungs; R17 migrates this surface to the train zone,
	// which owns the column once the routes physically move), and, for an estate admin, the
	// governance and operations surfaces over it. Grouping by DOMAIN rather than by zone is what
	// keeps a growing product from growing the bar: a new ROUTE becomes a row in a panel column —
	// only a new ZONE earns a new entry (R15). The project switcher sits at the head of the bar on
	// every zone (global context belongs in global chrome).
	const lakehouse: TopNavGroup[] = [
		{ label: 'Catalog', items: DATA_ITEMS },
		{ label: 'Models', items: MODEL_ITEMS },
		// Lineage is an AREA of this zone (/lakehouse/lineage), exactly like Models
		// (/lakehouse/models) — so it is a column, not a trigger of its own. It used to be top-level,
		// which forced the Lakehouse trigger to carve lineage out of its own match to stop both
		// lighting up, and left a bar where one entry was a zone and another was an area inside it
		// with no way for a reader to tell why. Trigger = zone, column = area; the bar is now
		// Lakehouse + Media, and a new route is a row in a column.
		{ label: 'Lineage', items: LINEAGE_ITEMS },
	];
	if (estateAdmin) {
		lakehouse.push(
			{ label: 'Governance', items: GOVERNANCE_ITEMS },
			{ label: 'Operations', items: OPERATIONS_ITEMS },
		);
	}
	return [
		// NO "Home" ENTRY. The origin root is reachable two better ways already — the project
		// switcher at the head of this same row, and the sidebar header, which names the zone you are
		// in and links home. A third control to the same destination is noise in a bar whose job is
		// to move you BETWEEN zones, and it made the estate's landing surface look like a peer of
		// Lakehouse and Compute rather than the thing containing them.
		{
			title: 'Lakehouse',
			href: '/lakehouse/catalog',
			icon: Database,
			// The whole merged zone — catalog, models, lineage, and (for an admin) governance and
			// operations. No carve-out: every area is a column of this one trigger.
			match: under('/lakehouse'),
			groups: lakehouse,
			tier: 'primary',
		},
		{
			// COMPUTE is the Ray/job plane — the merged rask zone. The old overview zone folded in
			// here (R16), so the zone root is the overview and the panel lists the Ray surfaces.
			title: 'Compute',
			href: '/compute/',
			icon: Cpu,
			match: under('/compute'),
			items: [...COMPUTE_ITEMS],
			tier: 'primary',
		},
		{
			// THE global workbench — its own ZONE (open_workbench.md): one dock composing panels the
			// other zones build and serve as custom elements, plus saved views. Primary: cross-zone
			// work happens here, beside the lakehouse it reads and the compute it watches.
			title: 'Workbench',
			href: '/workbench/',
			icon: LayoutDashboard,
			match: under('/workbench'),
			tier: 'primary',
		},
		{
			// SEARCH is the media read plane — the viewer. Named for what it is FOR, not for the
			// directory it lives in: a person looking for a moment in the corpus is searching, and
			// "Media" described our folder layout rather than their task.
			title: 'Search',
			href: '/media/',
			icon: Search,
			match: under('/media'),
			items: [...MEDIA_ITEMS],
		},
		{
			// ANNOTATE is its own microfrontend (/annotator) and its own job: the write plane over the
			// same corpus Search reads. One trigger per zone is the rule, so it is a trigger — it was
			// briefly a row inside Search's panel, which broke that rule and buried the labeling
			// workflow one hover deep. A single surface, so a plain link rather than a panel.
			title: 'Annotate',
			href: '/annotator/',
			icon: PenLine,
			match: under('/annotator'),
		},
		{
			// TRAIN is its own zone again (R17): submit, watch, monitor and analyse training, plus the
			// model registry that migrates over from Lakehouse. The zone is being scaffolded — the
			// entry rides the bar NOW (R15: a zone missing from the navbar is a defect regardless of
			// scaffold status); a plain link until its areas are real enough to panel.
			title: 'Train',
			href: '/train/',
			icon: Brain,
			match: under('/train'),
		},
		{
			// STUDIO stays the sandbox/PoC zone (R17) — one experimental surface, so a plain link.
			title: 'Studio',
			href: '/studio/',
			icon: FlaskConical,
			match: under('/studio'),
		},
	];
}

/** The first path segment = the owning zone ('' = the home zone at the origin root). A link whose
 *  zone differs from the current pathname's leaves this app's route manifest, so it must hard-nav
 *  (data-sveltekit-reload); same-zone links stay soft for SPA speed. */
export const zoneOf = (p: string) => p.split('/').filter(Boolean)[0] ?? '';

const prefetched = new Set<string>();

/** Warm a CROSS-ZONE target document on intent (hover/focus). SvelteKit's own
 *  `data-sveltekit-preload-data` only helps same-zone soft navs — a cross-zone link is a full
 *  document load into another app — so we drop a `<link rel="prefetch">` for the target instead.
 *  Honest scope: this is a browser HINT that warms the HTTP cache for the target zone's document
 *  (Chromium and Firefox honor it; Safari does not), so the hard nav paints from cache instead of
 *  a cold round-trip. Once per href per document; SSR no-op. */
export function prefetchDocument(href: string) {
	if (typeof document === 'undefined' || prefetched.has(href)) return;
	prefetched.add(href);
	const link = document.createElement('link');
	link.rel = 'prefetch';
	link.href = href;
	document.head.append(link);
}

/** `{@attach}` factory: warm `href` (prefetchDocument) on pointerenter/focus. Native listeners on
 *  purpose — inside a `child({ props })` snippet the component's own spread props may carry their
 *  own pointer handlers (e.g. the sidebar MenuButton's tooltip trigger), and a plain
 *  `onpointerenter` attribute would be overwritten by (or overwrite) them. */
export function prefetchOnIntent(href: string) {
	return (el: HTMLElement) => {
		const warm = () => prefetchDocument(href);
		el.addEventListener('pointerenter', warm);
		el.addEventListener('focus', warm);
		return () => {
			el.removeEventListener('pointerenter', warm);
			el.removeEventListener('focus', warm);
		};
	};
}
