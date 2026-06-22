import { House, Server, FileText, LayoutGrid, Database } from '@lucide/svelte';

/** All lucide icons share one component signature, so any icon's type fits. */
type IconComponent = typeof Server;

/** A leaf route inside a collapsible domain item. */
export type NavLeaf = {
	title: string;
	/** ABSOLUTE href incl. the owning app's kit.paths.base (e.g. /compute/overview). */
	href: string;
	/** Active predicate vs the FULL pathname (which includes the base). */
	match: (p: string) => boolean;
};

/** A top-level sidebar item — a domain. With `items` it renders as a collapsible
 *  accordion (sidebar-07 NavMain); without, as a direct link. */
export type NavItem = {
	title: string;
	icon: IconComponent;
	/** Landing href (the item links here when it has no sub-routes / on the domain). */
	href: string;
	/** Active predicate vs the FULL pathname — also controls accordion default-open. */
	match: (p: string) => boolean;
	items?: NavLeaf[];
};

/** A selectable project — the sidebar header switcher. Backend support is pending;
 *  there is one implicit "Default" project today. */
export type Project = { name: string; subtitle?: string };

/** The footer profile identity. No auth yet, so this is a local placeholder. */
export type NavUser = { name: string; email?: string; initials?: string };

/** prefix-segment match: active when the path equals the href or is nested under it. */
const seg = (href: string) => (p: string) => p === href || p.startsWith(href + '/');
/** domain match: active when the path is under any of the given prefixes. */
const under =
	(...prefixes: string[]) =>
	(p: string) =>
		prefixes.some((pre) => p === pre || p.startsWith(pre + '/'));

/**
 * Single source of truth for the sidebar nav, in the sidebar-07 NavMain shape:
 * domain items that expand (accordion) to their routes. Hrefs are absolute +
 * base-prefixed so the same nav renders identically in every app and routes
 * cross-app. Compute + Storage own their bases; Documents + Batches still live in
 * the catch-all (no base) until carved out.
 */
export const navMain: NavItem[] = [
	// Home / project picker — the composed root (/), served by the catch-all app.
	{ title: 'Home', icon: House, href: '/', match: (p) => p === '/' },
	{
		// The Compute landing IS the overview (served at /compute) — clicking "Compute"
		// shows it; the sub-routes are the rest of the cluster views (no separate Overview).
		title: 'Compute',
		icon: Server,
		href: '/compute',
		match: under('/compute'),
		items: [
			{ title: 'Cluster', href: '/compute/cluster', match: seg('/compute/cluster') },
			{ title: 'Jobs', href: '/compute/jobs', match: seg('/compute/jobs') },
			{ title: 'Actors', href: '/compute/actors', match: seg('/compute/actors') },
			{ title: 'Logs', href: '/compute/logviewer', match: seg('/compute/logviewer') },
			{ title: 'Serve', href: '/compute/serve', match: seg('/compute/serve') },
			{ title: 'API', href: '/compute/api-docs', match: seg('/compute/api-docs') },
		],
	},
	{
		title: 'Discover',
		icon: FileText,
		href: '/search',
		match: under('/search', '/browse', '/viewer'),
		items: [
			{ title: 'Search', href: '/search', match: seg('/search') },
			{ title: 'Viewer', href: '/viewer', match: seg('/viewer') },
			{ title: 'Browse', href: '/browse', match: seg('/browse') },
		],
	},
	{
		title: 'Batches',
		icon: LayoutGrid,
		href: '/batches',
		match: seg('/batches'),
	},
	{
		title: 'Storage',
		icon: Database,
		href: '/storage',
		match: seg('/storage'),
	},
];
