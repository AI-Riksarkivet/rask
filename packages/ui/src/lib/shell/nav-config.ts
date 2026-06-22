import {
	Gauge,
	Server,
	ServerCog,
	Boxes,
	ListTree,
	FileText,
	Search,
	Library,
	Image,
	LayoutGrid,
	BookOpen,
	Database,
} from '@lucide/svelte';

/** All lucide icons share one component signature, so any icon's type fits. */
type IconComponent = typeof Gauge;

export type NavItem = {
	title: string;
	/**
	 * ABSOLUTE href including the owning app's `kit.paths.base`, so a link routes
	 * to the right microfrontend through the composition proxy (e.g. compute items
	 * are `/compute/*`, storage is `/storage`). Within the owning app SvelteKit
	 * handles it client-side; across apps it is a full-page navigation.
	 */
	href: string;
	icon: IconComponent;
	/** Active-route predicate against the FULL pathname (which includes the base). */
	match: (p: string) => boolean;
};

export type NavGroup = {
	label: string;
	/** The owning app's base prefix (''=catch-all, '/compute', '/storage', …). */
	base: string;
	items: NavItem[];
};

/** A selectable project — the sidebar header switcher. Backend support is pending;
 *  there is one implicit "Default" project today. */
export type Project = { name: string; subtitle?: string };

/** The footer profile identity. No auth yet, so this is a local placeholder. */
export type NavUser = { name: string; email?: string; initials?: string };

/** prefix-segment match: active when the path equals the href or is nested under it. */
const seg = (href: string) => (p: string) => p === href || p.startsWith(href + '/');

/**
 * Single source of truth for the sidebar nav. Hrefs are absolute + base-prefixed
 * so the same nav renders identically in every app and routes cross-app correctly.
 * Compute + Storage own their bases; Documents + Batches still live in the catch-all
 * (base '') until they are carved out — update their base then.
 */
export const navGroups: NavGroup[] = [
	{
		label: 'Compute',
		base: '/compute',
		items: [
			{
				title: 'Overview',
				href: '/compute/overview',
				icon: Gauge,
				match: seg('/compute/overview'),
			},
			{ title: 'Cluster', href: '/compute/cluster', icon: Server, match: seg('/compute/cluster') },
			{ title: 'Jobs', href: '/compute/jobs', icon: ListTree, match: seg('/compute/jobs') },
			{ title: 'Actors', href: '/compute/actors', icon: Boxes, match: seg('/compute/actors') },
			{
				title: 'Logs',
				href: '/compute/logviewer',
				icon: FileText,
				match: seg('/compute/logviewer'),
			},
			{ title: 'Serve', href: '/compute/serve', icon: ServerCog, match: seg('/compute/serve') },
			{ title: 'API', href: '/compute/api-docs', icon: BookOpen, match: seg('/compute/api-docs') },
		],
	},
	{
		label: 'Documents',
		base: '',
		items: [
			{ title: 'Search', href: '/search', icon: Search, match: seg('/search') },
			{ title: 'Viewer', href: '/viewer', icon: Image, match: seg('/viewer') },
			{ title: 'Browse', href: '/browse', icon: Library, match: seg('/browse') },
		],
	},
	{
		label: 'Batches',
		base: '',
		items: [{ title: 'Batches', href: '/batches', icon: LayoutGrid, match: seg('/batches') }],
	},
	{
		label: 'Storage',
		base: '/storage',
		items: [{ title: 'S3', href: '/storage', icon: Database, match: seg('/storage') }],
	},
];
