import { Database } from '@lucide/svelte';
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
	/** ABSOLUTE, domain-relative href (e.g. /data/tables). */
	href: string;
	/** Active predicate vs the FULL pathname. */
	match: (p: string) => boolean;
	icon?: IconComponent;
	/** True for a leaf that leaves this zone's route manifest (e.g. media's Annotate → /annotator):
	 *  the sidebar link then hard-navigates (data-sveltekit-reload) instead of soft-routing. */
	reload?: boolean;
};

/** The per-zone sidebar config: each zone passes ITS OWN routes to the shared AppShell. The zone
 *  list itself lives in the top navbar (`topNav`) — the sidebar never renders other zones. */
export type ZoneNav = {
	/** The zone's display name — the sidebar group label (e.g. "Data"). */
	title: string;
	leaves: ZoneNavLeaf[];
};

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
	/** The zone's sub-areas. Present → the navbar renders a NavigationMenu trigger opening a panel
	 *  of these; absent → a plain link, because the zone has a single surface and a dropdown with
	 *  one row in it would be noise. Deliberately a SUBSET of the zone's own sidebar (`ZoneNav`):
	 *  this is the cross-zone jump list, not a mirror of in-zone navigation. */
	items?: TopNavItem[];
	/** Grouped alternative to `items` — rendered as labelled columns. Used by Lakehouse, whose panel
	 *  spans the catalog, the model registry and the governance surfaces. */
	groups?: TopNavGroup[];
};

const DATA_ITEMS: TopNavItem[] = [
	{
		title: 'Projects',
		href: '/lakehouse/data/projects',
		description: 'Tenants, their warehouses and their members.',
	},
	{ title: 'Tables', href: '/lakehouse/data/tables', description: 'The governed table registry.' },
	{
		title: 'Namespaces',
		href: '/lakehouse/data/namespaces',
		description: 'Medallion namespaces and their maintenance policies.',
	},
	{
		title: 'Warehouses',
		href: '/lakehouse/data/warehouses',
		description: 'Storage bindings — one bucket per project.',
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
	{ title: 'Search', href: '/media', description: 'Semantic search over the corpus.' },
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
		href: '/lakehouse/admin/access',
		description: 'The FGA workbench: check, tuples, graph.',
	},
	{
		title: 'Tenants',
		href: '/lakehouse/admin/tenants',
		description: 'Warehouses per project, and who administers them.',
	},
	{
		title: 'Audit',
		href: '/lakehouse/admin/audit',
		description: 'The compliance trail — who did what.',
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
 * The top-navbar IA. There are four zones now — home, lakehouse, media, annotator — and the bar shows
 * three entries over them, because Lakehouse and Lineage are two views of the ONE merged estate zone
 * rather than two apps. That is the point of the merge: a hop from the catalog to the lineage graph, or
 * to governance, is a soft navigation inside one router; only Media and Annotate still cross a zone
 * boundary and hard-navigate. The sidebar renders the current AREA's routes (`ZoneNav`).
 *
 * A zone with sub-areas carries `items`, and the navbar renders it as a NavigationMenu trigger with
 * a panel — so the estate's shape is reachable from any zone in one hop instead of landing on a
 * zone root and hunting through its sidebar. Zones with a single surface stay plain links.
 *
 * Admin appends ONLY for an estate admin (`me.estate_admin` from the frozen `/v1/me` contract) —
 * fail-closed: an unresolved/absent `me` renders the base entries. Access is NOT a top-level
 * entry: it lives inside the admin zone (/admin/access), so Admin covers the whole /admin subtree
 * here and Access appears only as one row of Admin's panel.
 */
export function topNav(estateAdmin: boolean): TopNavEntry[] {
	// LAKEHOUSE gathers everything that describes or governs the one governed estate: the catalog
	// (projects → warehouses → namespaces → tables), the model registry (models are catalog objects
	// too — models$<model> carries the same rungs), and, for an estate admin, the governance and
	// operations surfaces over it. Grouping by DOMAIN rather than by zone is what keeps the bar at
	// three words while the product grows: a new route becomes a row in a panel column, never a new
	// top-level entry. Home is the product mark, not a nav item; the project switcher sits at the
	// head of the bar on every zone (global context belongs in global chrome).
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
		{
			title: 'Lakehouse',
			href: '/lakehouse/data',
			// The whole merged zone — catalog, models, lineage, and (for an admin) governance and
			// operations. No carve-out: every area is a column of this one trigger.
			match: under('/lakehouse'),
			groups: lakehouse,
		},
		{
			// SEARCH is the media read plane — the viewer. Named for what it is FOR, not for the
			// directory it lives in: a person looking for a moment in the corpus is searching, and
			// "Media" described our folder layout rather than their task.
			title: 'Search',
			href: '/media',
			match: under('/media'),
			items: [...MEDIA_ITEMS],
		},
		{
			// ANNOTATE is its own microfrontend (/annotator) and its own job: the write plane over the
			// same corpus Search reads. One trigger per zone is the rule, so it is a trigger — it was
			// briefly a row inside Search's panel, which broke that rule and buried the labeling
			// workflow one hover deep. A single surface, so a plain link rather than a panel.
			title: 'Annotate',
			href: '/annotator',
			match: under('/annotator'),
		},
		// COMPUTE (rask) lands here as the fourth trigger when that merge happens — the Ray/job plane.
		// Deliberately NOT rendered yet: a trigger with nowhere to go is worse than a missing one.
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
