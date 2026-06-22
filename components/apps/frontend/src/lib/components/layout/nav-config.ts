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
} from 'lucide-svelte';

/** All lucide icons share one component signature, so any icon's type fits. */
type IconComponent = typeof Gauge;

export type NavItem = {
	title: string;
	href: string;
	icon: IconComponent;
	/** Active-route predicate against the current pathname. */
	match: (p: string) => boolean;
	/**
	 * Optional named action run on click *instead of* navigating to `href`
	 * (handled in app-sidebar.svelte). Used for the Viewer "open a random
	 * completed batch" shortcut, which has no static index route.
	 */
	actionId?: 'viewer-random';
	/** Cross-MFE target — render as a full-page navigation, never client-side goto. */
	external?: boolean;
};

export type NavGroup = {
	label: string;
	items: NavItem[];
};

/** Single source of truth for the sidebar nav. Icons reuse the old rail's glyphs. */
export const navGroups: NavGroup[] = [
	{
		label: 'Compute',
		items: [
			{
				title: 'Overview',
				href: '/overview',
				icon: Gauge,
				match: (p) => p.startsWith('/overview'),
			},
			{ title: 'Cluster', href: '/cluster', icon: Server, match: (p) => p.startsWith('/cluster') },
			{ title: 'Jobs', href: '/jobs', icon: ListTree, match: (p) => p.startsWith('/jobs') },
			{ title: 'Actors', href: '/actors', icon: Boxes, match: (p) => p.startsWith('/actors') },
			{
				title: 'Logs',
				href: '/logviewer',
				icon: FileText,
				match: (p) => p.startsWith('/logviewer'),
			},
			{ title: 'Serve', href: '/serve', icon: ServerCog, match: (p) => p.startsWith('/serve') },
			{ title: 'API', href: '/api-docs', icon: BookOpen, match: (p) => p.startsWith('/api-docs') },
		],
	},
	{
		label: 'Documents',
		items: [
			{ title: 'Search', href: '/search', icon: Search, match: (p) => p.startsWith('/search') },
			{
				title: 'Viewer',
				href: '/batches',
				icon: Image,
				match: (p) => p.startsWith('/viewer'),
				actionId: 'viewer-random',
			},
			{ title: 'Browse', href: '/browse', icon: Library, match: (p) => p.startsWith('/browse') },
		],
	},
	{
		label: 'Batches',
		items: [
			{ title: 'Batches', href: '/batches', icon: LayoutGrid, match: (p) => p === '/batches' },
		],
	},
	{
		label: 'Storage',
		items: [{ title: 'S3', href: '/s3', icon: Database, match: (p) => p.startsWith('/s3') }],
	},
];
