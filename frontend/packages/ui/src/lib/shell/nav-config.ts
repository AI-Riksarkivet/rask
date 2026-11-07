import { Database, FolderKanban, House, Settings } from '@lucide/svelte';

/** All lucide icons share one component signature, so any icon's type fits. */
export type IconComponent = typeof Database;

/** One entry of the reader's activity log, as the bell renders it: a write the app sent and its
 *  outcome. Declared structurally, so the app's own store satisfies it without importing from here.
 *  A failure's `status` is the HTTP status the door answered (0: unreachable), or `null` when the
 *  write failed before any answer. */
export type ActivityEntryLike = { id: string; label: string; at: number } & (
	| { ok: true }
	| { ok: false; status: number | null; detail: string }
);

/** What the app hands the shell to light up the navbar's bell: the reader's own activity, newest
 *  first, which the app keeps. The shell never stores it; opening the panel and clearing the log come
 *  back through the callbacks. */
export type ActivityFeed = {
	entries: readonly ActivityEntryLike[];
	/** Entries recorded since the panel was last opened. */
	unread: number;
	/** The panel opened: the app marks every entry seen. */
	onopen?: () => void;
	/** The reader asked to clear the log. */
	onclear?: () => void;
};

/** A leaf route in the sidebar. */
export type PageNavLeaf = {
	title: string;
	/** ABSOLUTE, domain-relative href (e.g. /catalog/tables). */
	href: string;
	/** Active predicate vs the FULL pathname. */
	match: (p: string) => boolean;
	icon?: IconComponent;
	/** Sub-routes, rendered as a `Sidebar.MenuSub` under this leaf and auto-expanded while any of
	 *  them (or the parent) is active. One level only — a third tier is a sign the PAGE should have
	 *  been split, not the menu. */
	children?: PageNavLeaf[];
	/** WHY this area cannot be opened for the thing currently in view — a short phrase, or absent
	 *  when it can.
	 *
	 *  A leaf carrying this renders DISABLED rather than being dropped from the rail. The estate
	 *  used to hide what a corpus could not do, which is fail-closed and defensible, and it reads to
	 *  a user as "this product has no knowledge graph" rather than "THIS corpus has no knowledge
	 *  graph" — two very different statements, and only the second is true. A greyed row with the
	 *  reason in its tooltip says which.
	 *
	 *  It covers PERMISSION as well as capability (#143). It did not: the rule was "a leaf someone may
	 *  not SEE is still absent", which left every authorization gate hiding its rows. That reads to the
	 *  denied user as a product without the feature, so they never ask for the grant — the same defect
	 *  as the corpus case, with a worse remedy available, because a permission denial can name the
	 *  exact relation to request. Carry the FGA denial's own wording and the tooltip becomes an
	 *  actionable request rather than a dead end.
	 *
	 *  It is still not the DOOR. The route's own gate stays fail-closed and unchanged; this only
	 *  decides whether the rail admits the route exists. A disabled row cannot navigate. */
	unavailable?: string;
};

/** One labelled section of the sidebar — a `Sidebar.Group` with its own `GroupLabel`. */
export type PageNavGroup = {
	/** The section heading (e.g. "Catalog", "Governance"). */
	label: string;
	items: PageNavLeaf[];
	/** Collapsible via the label (default true). Set false to pin a section open. */
	collapsible?: boolean;
	/** Start collapsed. Ignored while the group contains the active route — a section that hides
	 *  where you currently are reads as a broken sidebar. */
	defaultCollapsed?: boolean;
};

/** The ROOT row — rendered above every group, with no group label of its own: the landing that
 *  SUMMARISES the sections below it rather than a member of one. */
export type PageNavRoot = PageNavLeaf;

/** The sidebar config the app passes to the shared AppShell, GROUPED into labelled sections so the
 *  whole surface is visible at once. */
export type PageNav = {
	/** The display name — shown as the sidebar's own heading. */
	title: string;
	/** The landing row, above and outside every group. See {@link PageNavRoot}. */
	root?: PageNavRoot;
	groups: PageNavGroup[];
	/** PINNED TO THE BOTTOM, in `Sidebar.Footer` — it does not scroll with `groups`.
	 *
	 *  For a surface that is not one of the AREAS but a place you go to work across them: the
	 *  dock is the case this exists for. Rendered by the same `PageNav` component as the rail (the
	 *  sidebar hands it a synthetic one-group nav), so a footer leaf keeps active-state matching and
	 *  its tooltip, and `pageNavLeaves` walks it. */
	footer?: PageNavGroup;
};

/** Every leaf in a nav, flattened depth-first (parents before their children). The shell uses it to
 *  decide whether a rail is worth rendering. */
export function pageNavLeaves(nav: PageNav | null | undefined): PageNavLeaf[] {
	if (!nav) return [];
	const out: PageNavLeaf[] = [];
	if (nav.root) {
		out.push(nav.root);
		if (nav.root.children) out.push(...nav.root.children);
	}
	// The footer group is nav, not chrome — walk it too.
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

/** The navbar profile identity — populated from the OIDC session. */
export type NavUser = { name: string; email?: string; initials?: string };

/** One project membership row from `/management/v1/me` — the tenant and the caller's role in it. */
export type MeProject = { project: string; role: 'admin' | 'member' };

/** The frozen `GET /management/v1/me` identity contract, mirrored structurally from @rask/api (the shared shell
 *  never imports app data — same seam as `NavUser`): any verified caller's sub/name/email, whether
 *  they hold the estate-admin privilege, and their project memberships. */
export type Me = {
	sub: string;
	name: string | null;
	email: string | null;
	estate_admin: boolean;
	projects: MeProject[];
};

/** Drop a single trailing slash (except on root "/"), so every matcher is trailing-slash-robust. */
export const norm = (p: string) => (p.length > 1 && p.endsWith('/') ? p.slice(0, -1) : p);
/** prefix-segment match: active when the path equals the href or is nested under it. */
export const seg = (href: string) => (p: string) =>
	norm(p) === href || norm(p).startsWith(href + '/');
/** exact match: active ONLY on this exact path. Used for a root leaf whose href is a prefix of its
 *  siblings' (Overview=/catalog) — `seg` there would light Overview up on /catalog/tables too. */
export const exact = (href: string) => (p: string) => norm(p) === href;
/** domain match: active when the path is under any of the given prefixes. */
export const under =
	(...prefixes: string[]) =>
	(p: string) =>
		prefixes.some((pre) => norm(p) === pre || norm(p).startsWith(pre + '/'));

/** One sub-area inside a navbar panel, with a one-line description so the panel explains the estate
 *  instead of just listing words. */
export type TopNavItem = { title: string; href: string; description: string };

/** A labelled column inside a navbar panel: a trigger that gathers SEVERAL concerns (Lakehouse:
 *  workspace + catalog + operations) groups its rows under headings. */
export type TopNavGroup = { label: string; items: TopNavItem[] };

/** One top-navbar entry. */
export type TopNavEntry = {
	title: string;
	href: string;
	match: (p: string) => boolean;
	/** The entry's mark in the bar (lucide) — same signature seam as `PageNavLeaf.icon`. */
	icon?: IconComponent;
	/** The entry's sub-areas. Present → the navbar renders a NavigationMenu trigger opening a panel of
	 *  these; absent → a plain link. */
	items?: TopNavItem[];
	/** Grouped alternative to `items` — rendered as labelled columns. */
	groups?: TopNavGroup[];
	/** Visual weight in the in-project bar: 'primary' leads it. */
	tier?: 'primary' | 'secondary';
	/** WHY this entry cannot be opened by the current identity — a short phrase, or absent when it can.
	 *
	 *  The `PageNavLeaf.unavailable` contract, one level up (#143). An entry carrying it renders as a
	 *  disabled, non-navigating row with the reason in its tooltip, rather than being dropped from the
	 *  bar: a bar that silently loses Settings tells a non-admin the estate has no settings, when the
	 *  true statement is that THEY cannot open them — and only the second tells them what to ask for. */
	unavailable?: string;
};

// HIERARCHY ORDER — project > warehouse > namespace > table, identical to the sidebar's Catalog group.
// No Projects row: a project is the TOP of the hierarchy, so its list is the main menu's.
const DATA_ITEMS: TopNavItem[] = [
	{
		title: 'Warehouses',
		href: '/catalog/warehouses',
		description: "Every project's storage bindings — one bucket per warehouse.",
	},
	{
		title: 'Namespaces',
		href: '/catalog/namespaces',
		description: 'Medallion namespaces and their maintenance policies.',
	},
	{
		title: 'Tables',
		href: '/catalog/tables',
		description: 'The governed table registry.',
	},
	{
		title: 'Storage',
		href: '/catalog/storage',
		description: 'The registered stores, by medallion tier.',
	},
];

/** The PLATFORM panel — the Settings entry's rows. Each reads across every project and takes none,
 *  which is why they are settings. The same three rows, in the same order and under the same names, as
 *  `/settings` itself renders. Estate-admin only. */
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

/** The Lakehouse panel's first column. `TopNavGroup` has no root slot, so the overview is a row. */
const WORKSPACE_ITEMS: TopNavItem[] = [
	{
		title: 'Overview',
		href: '/catalog',
		description: "The active project's hierarchy and the warehouses that claim it.",
	},
	{
		title: 'Workbench',
		href: '/workbench',
		description: 'Events and tables in one arrangeable dock.',
	},
];

const OPERATIONS_ITEMS: TopNavItem[] = [
	{ title: 'Events', href: '/admin/events', description: 'The live control-event feed.' },
	{
		title: 'Streams',
		href: '/admin/streams',
		description: 'JetStream consumers and their lag.',
	},
	{
		title: 'DLQ',
		href: '/admin/dlq',
		description: 'Dead-lettered lineage runs, with replay.',
	},
];

/**
 * The MAIN MENU's bar (ruling 2026-08-03): Home, Projects and Settings — the places you can BE at the
 * estate level, peers, so no tiers.
 *
 * Settings is estate-admin only, and since #143 that shows as a DISABLED entry rather than an absent
 * one: a bar which silently loses Settings tells a non-admin the estate has no settings. Disabled with
 * the reason in the tooltip is the only version of this bar that is TRUE for both identities.
 */
export function mainMenuNav(estateAdmin: boolean, deniedReason?: string): TopNavEntry[] {
	return [
		{
			title: 'Home',
			href: '/',
			icon: House,
			// EXACT: '/' must not light up on /projects or /settings — they are its SIBLINGS.
			match: exact('/'),
		},
		PROJECTS_ENTRY,
		// The default is the FGA denial's own wording, so the bar, the route's 403 and the API all name
		// the same relation on the same object. `deniedReason` tells an anonymous visitor to sign in,
		// where a signed-in non-admin must ask for the grant.
		estateAdmin
			? SETTINGS_ENTRY
			: {
					// `items` is DROPPED, not merely left unrendered: showing the entry must not hand out its
					// children (`/settings/access` reads the platform's whole tuple store).
					...SETTINGS_ENTRY,
					items: undefined,
					unavailable:
						deniedReason ?? 'estate-admin only (needs can_observe_events on the FGA root)',
				},
	];
}

/**
 * Is this path the ESTATE level (the main menu) rather than the inside of a project?
 *
 * `/`, `/projects` and `/settings` are where you choose what to work on; everything else — the
 * lakehouse's pages, and `/projects/<id>` itself — is somewhere you have already chosen.
 */
export function isMainMenu(pathname: string): boolean {
	const p = norm(pathname);
	return (
		p === '' || p === '/' || p === '/projects' || p === '/settings' || p.startsWith('/settings/')
	);
}

/** The IN-PROJECT bar: the lakehouse, with its workspace, its catalog and — for an estate admin — its
 *  operations as columns of one panel. Who may do what is not here: that is platform configuration,
 *  under the main menu's Settings. */
export function topNav(estateAdmin: boolean): TopNavEntry[] {
	const lakehouse: TopNavGroup[] = [
		{ label: 'Workspace', items: WORKSPACE_ITEMS },
		{ label: 'Catalog', items: DATA_ITEMS },
	];
	if (estateAdmin) {
		lakehouse.push({ label: 'Operations', items: OPERATIONS_ITEMS });
	}
	return [
		{
			title: 'Lakehouse',
			href: '/catalog',
			icon: Database,
			match: under('/catalog', '/admin', '/workbench'),
			groups: lakehouse,
			tier: 'primary',
		},
	];
}

/** PROJECTS — the estate's list, and a plain link: the list, the per-project overview and the create
 *  flow are one surface. `under` so it stays lit on a project page. */
const PROJECTS_ENTRY: TopNavEntry = {
	title: 'Projects',
	href: '/projects',
	icon: FolderKanban,
	match: under('/projects'),
};

/** SETTINGS — PLATFORM configuration: users and roles, projects, the audit trail. */
const SETTINGS_ENTRY: TopNavEntry = {
	title: 'Settings',
	href: '/settings',
	icon: Settings,
	match: under('/settings'),
	items: [...PLATFORM_ITEMS],
};
