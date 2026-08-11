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
import type { InboxNotificationLike } from './inbox.js';
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
	/** The run rows, as the lineage service's `GET /runs` returns them — the ACTIVITY plane: every
	 *  run whose outputs you may read, whoever started it. Per-tab read state. */
	runs: RunStatusLike[];
	/** The INBOX plane: rows addressed to this subject, durable per subject. `undefined` means this
	 *  zone has no inbox transport — a different statement from an empty inbox, and the bell renders
	 *  it differently (no tabs, and the badge falls back to the run feed). */
	inbox?: InboxNotificationLike[];
	/** The server's unread count for the WHOLE inbox, not the page above. */
	inboxUnread?: number;
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
	/** WHY this area cannot be opened for the thing currently in view — a short phrase, or absent
	 *  when it can.
	 *
	 *  A leaf carrying this renders DISABLED rather than being dropped from the rail. The estate
	 *  used to hide what a corpus could not do, which is fail-closed and defensible, and it reads to
	 *  a user as "this product has no knowledge graph" rather than "THIS corpus has no knowledge
	 *  graph" — two very different statements, and only the second is true. A greyed row with the
	 *  reason in its tooltip says which.
	 *
	 *  It is not a substitute for authorization: a leaf someone may not SEE is still absent (the
	 *  admin Settings precedent). This is for capability, not permission. */
	unavailable?: string;
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
 *  sub-route (/models/experiments would light up Registry too), so those leaves match exactly. */
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
 *  catalog + models + lineage + operations) needs its rows grouped under headings, or the panel is just a
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
	 *  spans the catalog, the model registry, lineage and the operational surfaces. */
	groups?: TopNavGroup[];
	/** Visual weight in the ZONE bar. 'primary' is where the work happens — the lakehouse you govern
	 *  and the compute that fills it — and those lead the bar. Everything else is a real zone but a
	 *  destination you go to for a specific task, so it reads one step quieter. Six equally-weighted
	 *  entries give a newcomer no idea where to start; this is the ONLY thing that says so.
	 *  The MAIN MENU sets no tier and the shell renders none there: two or three peers are a group,
	 *  not a hierarchy, and a spacer between them reads as one entry having fallen off the row. */
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

const ANNOTATE_ITEMS: TopNavItem[] = [
	{ title: 'Canvas', href: '/annotator/', description: 'Label pages on the annotation canvas.' },
	{
		title: 'Browse',
		href: '/annotator/browse',
		description: 'The corpus, filtered to what needs labeling.',
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

/** The MODELS zone's panel rows — one model's whole life.
 *
 *  These were a COLUMN OF THE LAKEHOUSE PANEL (`/lakehouse/models/*`) until the surface itself moved.
 *  They now name the zone's own routes: the registry that training produces sits in the same place as
 *  the training that fills it, instead of one zone away from it.
 *
 *  TWO ROWS WERE DELETED HERE, and both had rotted the same way — a navbar row outliving its route:
 *
 *   - `Playground` → `/models/playground`. #131 moved inference to `/compute/inference` and the models
 *     zone never had (or kept) a playground route, so this row had been a 404 since that move.
 *   - `Pipeline` → `/models/pipeline`. The route existed but was a manual trigger for the medallion
 *     cascade head + train request — an operation on the lakehouse, not a fact about a model — and the
 *     cascade is event-driven, so the button was a second manual writer. Deleted 2026-08-07.
 *
 *  NOTHING CAUGHT EITHER ONE, and that is the part worth keeping. `nav-truth.test.ts` walked every
 *  zone's `ZoneNav` sidebar leaves and resolved each href against the route tree — but it did not see
 *  `TopNavItem` rows at all; `link-targets.ts` checks only a link's FIRST segment (`models` is served,
 *  so `/models/playground` passed); and the per-zone Playwright shell specs assert each row's `href`
 *  ATTRIBUTE, which a row pointing at a 404 satisfies perfectly. Three gates, all green, over a dead
 *  link in all seven zones.
 *
 *  So this stays a STANDING WARNING, not a note about the past: adding a row here is currently
 *  unguarded, and the fix is to run THIS file through `nav-truth.test.ts`'s existing scanner and route
 *  resolver — the same two it already applies to every zone's sidebar leaves. Until that lands, the
 *  only thing standing between a new `TopNavItem` and a seven-zone 404 is whoever writes it. */
const MODEL_ITEMS: TopNavItem[] = [
	{ title: 'Registry', href: '/models/', description: 'Candidate → blessed, per model.' },
	{
		title: 'Experiments',
		href: '/models/experiments',
		description: 'Training runs and their metrics.',
	},
	{
		title: 'Training runs',
		href: '/models/runs',
		description: 'Every submitted run, live state first.',
	},
];

// NO `STUDIO_ITEMS`. Studio briefly panelled (Apps + Flows) and is a plain link again, because the
// launcher and the animation demo were deleted: the zone is ONE surface — the flow canvas at its
// root — and a dropdown with one row in it is noise. Same rule, opposite direction.

/** The PLATFORM panel — the Settings entry's rows. Each reads across every project and takes none,
 *  which is why they are settings rather than a zone's feature, and why they are SERVED by the home
 *  zone (#105): `/settings/access` and `/settings/audit` are its routes, `/projects` always was.
 *
 *  Deliberately the same three rows, in the same order and under the same names, as `/settings` itself
 *  renders — the navbar panel and the page below it are two controls answering one question, and the
 *  estate has been bitten before by letting them drift (see the lakehouse sidebar's own note). Estate-
 *  admin only. */
const PLATFORM_ITEMS: TopNavItem[] = [
	{
		title: 'Users & roles',
		href: '/settings/access',
		description: 'Who may do what, platform-wide: check, tuples, graph.',
	},
	{
		title: 'Projects',
		href: '/projects',
		description: 'Every project the platform knows, and who administers it.',
	},
	{
		title: 'Audit',
		href: '/settings/audit',
		description: 'The platform trail — who did what.',
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
	{
		// ETL — how data gets INTO the estate, and the one row here that never touches Ray. It was
		// absent while the zone's own sidebar carried it, so the two nav surfaces disagreed: a user
		// navigating by the NAVBAR could not reach the ingest plane at all, while one using the RAIL
		// could. Every other compute area appears in both, and that asymmetry is a large part of why
		// the plane was hard to find.
		title: 'ETL',
		href: '/compute/etl',
		description: 'Harvest a source into bronze — an S3 prefix, or a local directory.',
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
 * The top-navbar IA. Seven zones — home, lakehouse, explorer, annotator, compute, models, studio — and
 * the bar carries an entry for EVERY one of them (R15: a zone missing from the shared navbar is a
 * defect, regardless of scaffold status). Lakehouse and Lineage stay two views of the ONE merged
 * estate zone rather than two apps — a hop from the catalog to the lineage graph, or to the DLQ,
 * is a soft navigation inside one router; every OTHER entry crosses a zone boundary and
 * hard-navigates. The sidebar renders the current zone's routes (`ZoneNav`).
 *
 * A zone with sub-areas carries `items`, and the navbar renders it as a NavigationMenu trigger with
 * a panel — so the estate's shape is reachable from any zone in one hop instead of landing on a
 * zone root and hunting through its sidebar. Zones with a single surface stay plain links.
 *
 * The Operations column appends ONLY for an estate admin (`me.estate_admin` from the frozen `/v1/me`
 * contract) — fail-closed: an unresolved/absent `me` renders the base entries. Access is NOT a
 * top-level entry and never was: it is one row of the Settings panel (`/settings/access`), which is
 * where the surface itself now lives.
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
 * means a non-admin's main menu carries exactly TWO entries — Home and Projects (it said ONE while
 * Home was still absent from this bar). That is correct, not a degenerate case: the estate has that
 * much for them to choose between at this level, and the shell centres two entries as readily as it
 * centres three.
 *
 * NO TIERS here, unlike the zone bar. Weighting is for a row of eight zones, where "start at the
 * lakehouse" is real guidance; these two or three are the places you can BE at this level, peers by
 * construction, and the shell renders them as one uniform group (it also refuses to draw the tier
 * gap in this bar, so a re-tiered entry cannot punch a hole in it).
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
	// (projects → warehouses → namespaces → tables), the lineage over it, and, for an estate admin,
	// the operational surfaces on top. The model registry used to be a column here — models ARE
	// catalog objects (models$<model> carries the same rungs), which is why it sat here at all — but
	// R17's migration has now happened: the routes physically moved, so the column moved with them and
	// the MODELS zone owns it. A trigger claims a ZONE; it must not keep advertising another one's
	// routes. Grouping by DOMAIN rather than by zone is what
	// keeps a growing product from growing the bar: a new ROUTE becomes a row in a panel column —
	// only a new ZONE earns a new entry (R15). The project switcher sits at the head of the bar on
	// every zone (global context belongs in global chrome).
	const lakehouse: TopNavGroup[] = [
		// WORKSPACE first, and its own column rather than a row of Lineage. The dock composes the whole
		// zone (lineage graph + runs + events + the catalog's tables and object browser), so filing it
		// under one area is what made it invisible from the others — the placement that was reverted.
		{ label: 'Workspace', items: WORKSPACE_ITEMS },
		{ label: 'Catalog', items: DATA_ITEMS },
		// NO 'Models' COLUMN — the area left this zone for the MODELS zone, which is its own trigger
		// below. What the lakehouse keeps is model LINEAGE (which run wrote which version), and that is
		// already a Lineage row.
		//
		// Lineage is an AREA of this zone (/lakehouse/lineage) — so it is a column, not a trigger of
		// its own. It used to be top-level,
		// which forced the Lakehouse trigger to carve lineage out of its own match to stop both
		// lighting up, and left a bar where one entry was a zone and another was an area inside it
		// with no way for a reader to tell why. Trigger = zone, column = area; the bar is now
		// Lakehouse + Media, and a new route is a row in a column.
		{ label: 'Lineage', items: LINEAGE_ITEMS },
	];
	// OPERATIONS stays with the lakehouse; GOVERNANCE does not — by ruling (2026-08-03): "governance
	// and other setting stuff, more in terms of auth, should be part of the topnavbar when in main
	// menu, but under settings". Running THIS estate — its streams, its events, its dead letters — is
	// an operation on the lakehouse and belongs to the lakehouse. Who may do what — users & roles,
	// projects, the audit trail — is not a lakehouse feature at all; it is PLATFORM configuration, and
	// it lives under `Settings` below. It is in ONE place, not two: a Governance column here AND a
	// Settings entry there would be the same duplication the projects ruling deleted. Since #105 the
	// pages themselves are the home zone's too, so the row and the route finally agree.
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
			// The whole merged zone — catalog, models, lineage, and (for an admin) operations. No
			// carve-out: every area is a column of this one trigger.
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
			// workflow one hover deep. It carries a PANEL since #113: the "single surface" premise
			// expired when /browse landed — Canvas and Browse are two real destinations, and the
			// consistency rule is that every multi-surface zone panels (only Studio remains a link).
			title: 'Annotate',
			href: '/annotator/',
			icon: PenLine,
			match: under('/annotator'),
			items: [...ANNOTATE_ITEMS],
		},
		{
			// MODELS — the model-lifecycle zone. It began as R17's `train` (submit / runs / monitoring /
			// analysis, all scaffold) and was a PLAIN LINK on the reasoning that a one-row dropdown is
			// noise. It earns a panel now, because the migration R17 promised actually happened: the
			// registry and its experiments moved out of the lakehouse, so the zone has real destinations
			// rather than four placeholders. (It briefly had five rows; the pipeline door and the
			// playground are gone — see MODEL_ITEMS.)
			//
			// The label is the JOB, not the directory — and here they agree. "Train" named one AREA of
			// this zone and would now under-describe it: you come here to see what a model IS, not only
			// to make one.
			title: 'Models',
			href: '/models/',
			icon: Brain,
			match: under('/models'),
			items: [...MODEL_ITEMS],
		},
		{
			// STUDIO is the sandbox zone (R17) and a PLAIN LINK — one surface, the node-based flow
			// canvas at the zone root, so there is nothing for a dropdown to list.
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
};

/** SETTINGS — PLATFORM configuration: notifications, the defaults a NEW project is created with, and
 *  auth/authz. A real home-zone route (`/settings`), and since #105 so is every row of its panel —
 *  this entry once pointed at `/lakehouse/governance/access`, a placeholder for a page that did not
 *  exist, and then at the real page in another zone. Now the entry, the panel and the pages are one
 *  app.
 *
 *  Estate-admin only, and ABSENT rather than disabled for everyone else — the fail-closed rule the
 *  Governance column had before it moved here: a non-admin's bar must not even NAME a surface they
 *  are barred from, or the IA leaks the shape of the privilege. */
const SETTINGS_ENTRY: TopNavEntry = {
	title: 'Settings',
	href: '/settings',
	icon: Settings,
	// `under('/settings')` ALONE now. It also matched `/lakehouse/governance` while the workbench and
	// the audit trail were still served from there — the bar had to agree with itself across the seam.
	// #105 closed the seam: those routes are `/settings/access` and `/settings/audit`, the lakehouse
	// serves no `governance` segment at all, and a matcher for a path nothing serves is a claim that
	// can only ever be wrong.
	match: under('/settings'),
	items: [...PLATFORM_ITEMS],
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
export const HOME_ROUTES = ['projects', 'settings', 'preferences'];

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

/**
 * zone directory segment -> the label the NAVBAR uses for it.
 *
 * Derived from `topNav()` rather than written out, so it cannot drift from the bar: the bar is the
 * thing a person just read, and a breadcrumb that disagreed with it named the same zone twice on one
 * screen ("Annotate" above, "Annotator" below). `annotator` -> `Annotate` is the deliberate
 * label/directory split this exists for; every other zone happens to match, and would still be
 * correct if one stopped matching tomorrow.
 *
 * `estateAdmin` is irrelevant to labels — Settings is the only admin-gated entry and is not a zone —
 * so it is called with `false` and the result is stable.
 */
export function zoneLabels(): Record<string, string> {
	const out: Record<string, string> = {};
	for (const entry of topNav(false)) {
		const seg = entry.href.split('/').filter(Boolean)[0];
		if (seg) out[seg] = entry.title;
	}
	return out;
}
