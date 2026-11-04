import {
	Boxes,
	FolderKanban,
	Database,
	Gauge,
	HardDrive,
	LayoutDashboard,
	Inbox,
	Layers,
	Radio,
	ShieldCheck,
	Warehouse,
} from '@lucide/svelte';
import { exact, seg, under, type ZoneNav } from '@rask/ui/shell';

/**
 * The lakehouse's WHOLE surface, in one grouped sidebar: the catalog (what sits below a project —
 * warehouses › namespaces › tables, the stores and the held promotions), the operations an estate
 * admin runs, and the workbench pinned at the bottom.
 *
 * Who-may-do-what (access, audit) is NOT here: those surfaces read across every project and take
 * none, so they are platform settings (`/settings/`).
 */
/** The ROOT row — first leaf in the rail, above every group: the active project's overview. `exact`
 *  so it does not light up from inside every catalog page. */
const LAKEHOUSE_ROOT: ZoneNav['root'] = {
	title: 'Overview',
	href: '/catalog',
	match: exact('/catalog'),
	icon: Gauge,
};

const LAKEHOUSE_GROUPS: ZoneNav['groups'] = [
	{
		label: 'Catalog',
		items: [
			// HIERARCHY ORDER — project > warehouse > namespace > table, identical to the navbar panel's.
			{
				title: 'Warehouses',
				href: '/catalog/warehouses',
				match: seg('/catalog/warehouses'),
				icon: Warehouse,
			},
			{
				title: 'Namespaces',
				href: '/catalog/namespaces',
				match: seg('/catalog/namespaces'),
				icon: Boxes,
			},
			{
				title: 'Tables',
				href: '/catalog/tables',
				match: seg('/catalog/tables'),
				icon: Database,
			},
			{
				title: 'Storage',
				href: '/catalog/storage',
				match: seg('/catalog/storage'),
				icon: HardDrive,
			},
			{
				// A held promotion is a CATALOG concern, not a lineage one: the rung a decision is gated
				// on is `can_promote` on `namespace:<project>-<to_namespace>` (medallion promotions.py),
				// which is the catalog's own hierarchy.
				title: 'Promotions',
				href: '/catalog/promotions',
				match: seg('/catalog/promotions'),
				icon: ShieldCheck,
			},
		],
	},
	{
		// Running the estate — its streams, events and dead letters — behind the estate-admin door.
		label: 'Operations',
		items: [
			{
				title: 'Streams',
				href: '/admin/streams',
				match: seg('/admin/streams'),
				icon: Layers,
			},
			{
				title: 'Events',
				href: '/admin/events',
				match: seg('/admin/events'),
				icon: Radio,
			},
			{
				title: 'DLQ',
				href: '/admin/dlq',
				match: seg('/admin/dlq'),
				icon: Inbox,
			},
		],
	},
];

/** PINNED to the rail's bottom, outside `groups`. It composes the lineage event feed and the catalog's
 *  own table registry, so it belongs below the areas rather than among them. */
const LAKEHOUSE_FOOTER: ZoneNav['footer'] = {
	label: 'Workspace',
	items: [
		{
			title: 'Workbench',
			href: '/workbench',
			match: seg('/workbench'),
			icon: LayoutDashboard,
		},
	],
};

/** Groups behind the estate-admin door — the ones `/admin/*` serves. */
const PRIVILEGED_GROUPS = new Set(['Operations']);

/** The first path segment — `admin`, `catalog`, `workbench`, … — which decides the canvas layout. */
export function areaOf(pathname: string): string {
	return pathname.split('/').filter(Boolean)[0] ?? '';
}

/** Whether a path is one of the lakehouse's own pages, which carry its sidebar. */
export const inLakehouse = under('/catalog', '/admin', '/workbench');

/**
 * The lakehouse's sidebar, with anything the caller may not use rendered DISABLED — never dropped
 * (#143): a filtered row is indistinguishable from a product that has no such feature, while a
 * disabled one names the relation to ask for. The door itself is the route's own layout load, which
 * refuses regardless of what the rail shows.
 */
export function lakehouseSidebar(estateAdmin: boolean): ZoneNav {
	// The FGA denial's own wording, so the rail, the ForbiddenPage and the API's 403 all name the same
	// relation on the same object. A reason that paraphrases sends users looking for a setting.
	const denial = 'estate-admin only (needs can_observe_events on the FGA root)';
	return {
		title: 'Lakehouse',
		root: LAKEHOUSE_ROOT,
		groups: estateAdmin
			? LAKEHOUSE_GROUPS
			: LAKEHOUSE_GROUPS.map((g) =>
					PRIVILEGED_GROUPS.has(g.label)
						? {
								...g,
								// Children too: a parent may be disabled while its sub-routes stay live links,
								// which is exactly the hole a shallow copy would leave open.
								items: g.items.map((leaf) => ({
									...leaf,
									unavailable: denial,
									...(leaf.children
										? { children: leaf.children.map((c) => ({ ...c, unavailable: denial })) }
										: {}),
								})),
							}
						: g,
				),
		footer: LAKEHOUSE_FOOTER,
	};
}

// The ESTATE level's sidebar — ONE leaf, Projects (`under`, so it stays lit on `/projects/<project>`).
// One leaf is why the estate pages render NO rail: the shell suppresses the sidebar below two leaves,
// because a rail whose only row links to the page you are already on costs width and implies areas
// that do not exist.
export const ESTATE_NAV: ZoneNav = {
	title: 'Home',
	groups: [
		{
			label: 'Home',
			items: [
				{ title: 'Projects', href: '/projects', match: under('/projects'), icon: FolderKanban },
			],
		},
	],
};
