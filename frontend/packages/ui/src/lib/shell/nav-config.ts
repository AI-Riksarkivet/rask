import {
	Brain,
	Cpu,
	Database,
	FlaskConical,
	FolderKanban,
	House,
	PenLine,
	Search,
	Settings,
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
	/** True for a leaf that leaves this zone's route manifest (e.g. the explorer's Annotate → /annotator):
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
 *  too. Optional on purpose — a zone whose root genuinely belongs to a section (the explorer's Search
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
	/** PINNED TO THE BOTTOM, in `Sidebar.Footer` — it does not scroll with `groups`.
	 *
	 *  For a surface that is not one of the zone's AREAS but a place you go to work across them: the
	 *  dock is the case this exists for. Rendered by the same `ZoneNav` component as the rail (the
	 *  sidebar hands it a synthetic one-group nav), so a footer leaf keeps active-state matching, the
	 *  `reload` flag and its tooltip — and `zoneNavLeaves` walks it, which is what keeps
	 *  `dock-reachability` able to prove the dock is reachable from its own zone. */
	footer?: ZoneNavGroup;
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
	// The footer group is nav, not chrome — walk it too, or every gate that reads this list would
	// stop seeing the dock the moment it moved to the bottom of the rail.
	for (const group of [...nav.groups, ...(nav.footer ? [nav.footer] : [])]) {
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
// zone's dropdown inverted it. The list, the per-project overview and the provisioning flow now live
// at the TOP level in the home zone (`/projects`), carried by this bar's own `Projects` entry below;
// `/lakehouse/catalog/projects` no longer exists.
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

const EXPLORER_ITEMS: TopNavItem[] = [
	{ title: 'Search', href: '/explorer/', description: 'Semantic search over the corpus.' },
	{ title: 'Atlas', href: '/explorer/atlas', description: 'The embedding map of the corpus.' },
	{ title: 'Tree', href: '/explorer/tree', description: 'The corpus by topic hierarchy.' },
	{ title: 'Graph', href: '/explorer/graph', description: 'Relations between media entities.' },
	{
		// The dock lives INSIDE this zone (its panels are the zone's own components sharing one
		// search) — so it is a row of the zone's panel, exactly like every other area.
		title: 'Workbench',
		href: '/explorer/workbench',
		description: 'Results, atlas and player in one arrangeable dock.',
	},
	{ title: 'Workflow', href: '/explorer/workflow', description: 'The derivation pipeline.' },
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
 *  Search at /explorer). */
const WORKSPACE_ITEMS: TopNavItem[] = [
	{
		title: 'Workbench',
		href: '/lakehouse/workbench',
		description: 'Lineage, runs, events, tables and storage in one arrangeable dock.',
	},
];

const COMPUTE_ITEMS: TopNavItem[] = [
	{ title: 'Overview', href: '/compute/', description: 'The Ray plane at a glance.' },
	{
		// The zone's own dock — panels are this zone's components over its own remotes, so it is a row
		// of this panel like every other area of the zone.
		title: 'Workbench',
		href: '/compute/workbench',
		description: 'Jobs, capacity, actors and Serve in one arrangeable dock.',
	},
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
 * The top-navbar IA. Seven zones — home, lakehouse, explorer, annotator, compute, train, studio — and
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
/**
 * The MAIN MENU's bar, by ruling (2026-08-03): "we should only see 2 items in topnavbar — projects
 * and settings, nothing else".
 *
 * Standing at the estate root you are not moving between zones, you are choosing what to work on;
 * a full zone list there answers a question nobody asked yet and buries the two things that ARE the
 * main menu. The zone bar returns the moment you are inside a zone, which is where "move me to
 * another zone" is a real question.
 *
 * Settings is estate-admin only here for the same fail-closed reason it is everywhere else, which
 * means a non-admin's main menu carries exactly ONE entry. That is correct, not a degenerate case:
 * the estate has one thing for them to choose at that level.
 */
export function mainMenuNav(estateAdmin: boolean): TopNavEntry[] {
	// HOME leads the main menu — and appears ONLY here. The zone bar carries no Home entry (see
	// `topNav`): inside a project the way back up is the project switcher and the sidebar header, and
	// a third control to the same place is noise in a bar whose job is moving BETWEEN zones. At the
	// estate root the opposite holds — Home is not "a way back", it IS one of the three places you
	// can be, so it has to be nameable.
	return [
		{
			title: 'Home',
			href: '/',
			icon: House,
			// EXACT: '/' must not light up while you are on /projects or /settings — they are its
			// SIBLINGS at this level, not routes underneath it.
			match: exact('/'),
			tier: 'primary',
		},
		PROJECTS_ENTRY,
		...(estateAdmin ? [SETTINGS_ENTRY] : []),
	];
}

/**
 * Is this path the ESTATE level (the main menu) rather than the inside of a project?
 *
 * The two-level rule, in one predicate: `/`, `/projects` and `/settings` are where you choose what to
 * work on; everything else — every zone route, and `/projects/<id>` itself — is somewhere you have
 * already chosen. Opening a project is what puts you inside one, which is why the per-project page
 * gets the ZONE bar and the list above it does not.
 *
 * Deliberately a PATH test, not a stored "active project": scoping is by context, and this is the
 * context. A host-scoped deployment (`acme.localhost`, `projectFromHost`) simply arrives with a zone
 * path already, so it lands on the same answer without a second mechanism to keep in sync.
 */
export function isMainMenu(pathname: string): boolean {
	const p = norm(pathname);
	return (
		p === '' || p === '/' || p === '/projects' || p === '/settings' || p.startsWith('/settings/')
	);
}

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
		// WORKSPACE first, and its own column rather than a row of Lineage. The dock composes the whole
		// zone (lineage graph + runs + events + the catalog's tables and object browser), so filing it
		// under one area is what made it invisible from the others — the placement that was reverted.
		{ label: 'Workspace', items: WORKSPACE_ITEMS },
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
	// OPERATIONS stays with the lakehouse; GOVERNANCE does not — by ruling (2026-08-03): "governance
	// and other setting stuff, more in terms of auth, should be part of the topnavbar when in main
	// menu, but under settings". Running THIS estate — its streams, its events, its dead letters — is
	// an operation on the lakehouse and belongs to the lakehouse. Who may do what — access, tenants,
	// the audit trail — is not a lakehouse feature at all; it is estate-wide configuration, and it now
	// lives under `Settings` below. It is in ONE place, not two: a Governance column here AND a
	// Settings entry there would be the same duplication the projects ruling deleted.
	if (estateAdmin) {
		lakehouse.push({ label: 'Operations', items: OPERATIONS_ITEMS });
	}
	// THIS IS THE IN-PROJECT BAR — one entry per ZONE, and nothing else.
	//
	// NO "Home", NO "Projects", NO "Settings". Those three are the ESTATE level and live in
	// `mainMenuNav()`; putting them here too would make every zone's bar eleven entries long and blur
	// the one distinction this IA rests on — the estate root is where you choose what to work on, a
	// zone is where you do it. The way back up from inside a project is the project switcher and the
	// sidebar header, both of which sit in this same chrome.
	//
	// TIERS are a rendering signal, not a grouping: Lakehouse and Compute are where the work happens
	// — the lakehouse you govern and the compute that fills it — so they lead and the shell sets them
	// off with a gap. Everything after the gap is a task destination.
	return [
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
			// EXPLORER is the corpus read plane — the viewer. It was labelled "Search" while the
			// directory was `media`, on the reasoning that a label should name the task rather than
			// our folder layout. The zone is now `explorer` on every surface (path, package, image),
			// so the label and the directory finally agree, and "explore" is the wider truth of what
			// this zone does: search is ONE of its leaves, beside the atlas, the tree and the graph.
			title: 'Explorer',
			href: '/explorer/',
			icon: Search,
			match: under('/explorer'),
			items: [...EXPLORER_ITEMS],
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

/** PROJECTS — the estate's list. Not a zone: a route in the home zone (`/projects`,
 *  `/projects/<p>`), and a plain link because the list, the per-project overview and the create flow
 *  are one surface. `under` so it stays lit on a project page. */
const PROJECTS_ENTRY: TopNavEntry = {
	title: 'Projects',
	href: '/projects',
	icon: FolderKanban,
	match: under('/projects'),
	tier: 'primary',
};

/** SETTINGS — estate configuration: notifications, the defaults a NEW project is created with, and
 *  auth/authz. A real home-zone route (`/settings`), which is why this no longer points at
 *  `/lakehouse/governance/access` — that was a placeholder for a page that did not exist.
 *
 *  Estate-admin only, and ABSENT rather than disabled for everyone else — the fail-closed rule the
 *  Governance column had before it moved here: a non-admin's bar must not even NAME a surface they
 *  are barred from, or the IA leaks the shape of the privilege. */
const SETTINGS_ENTRY: TopNavEntry = {
	title: 'Settings',
	href: '/settings',
	icon: Settings,
	// Also lights on the governance routes the lakehouse still SERVES, so the bar agrees with itself
	// while those pages live at their old addresses. Moving them behind `/settings/` is a separate
	// change and needs redirects for anything already linking them.
	match: under('/settings', '/lakehouse/governance'),
	items: [...GOVERNANCE_ITEMS],
};

/**
 * First segments the CATCH-ALL home zone owns as REAL routes, rather than as another zone's base.
 *
 * `home` has no base path, so "first segment = zone" quietly misreads its own routes as zones of
 * their own: `/projects` looked like a `projects` zone. Harmless in one direction (a link from the
 * lakehouse still hard-navigated, which is correct) and wasteful in the other — standing on `/`, the
 * navbar's own Projects link cost a full document load to reach a route THIS app serves.
 *
 * Keep in step with `@rask/zone-contract`'s `HOME_ROUTES`, which enforces the same fact from the
 * other side (a link INTO these from another zone must carry `data-sveltekit-reload`). Two lists
 * because the gate cannot import the shell, and both are one line — a divergence shows up as a
 * cross-zone test failure rather than as silence.
 */
export const HOME_ROUTES = ['projects', 'settings'];

/** The owning ZONE of a path ('' = the home zone, which serves the origin root and `HOME_ROUTES`).
 *  A link whose zone differs from the current pathname's leaves this app's route manifest, so it
 *  must hard-nav (data-sveltekit-reload); same-zone links stay soft for SPA speed. */
export const zoneOf = (p: string) => {
	const first = p.split('/').filter(Boolean)[0] ?? '';
	return HOME_ROUTES.includes(first) ? '' : first;
};

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
