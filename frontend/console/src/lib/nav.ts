import {
	Boxes,
	Database,
	FolderKanban,
	HardDrive,
	House,
	Inbox,
	LayoutDashboard,
	Layers,
	Radio,
	Settings,
	ShieldCheck,
	Warehouse,
} from '@lucide/svelte';
import type { PageNav, PageNavLeaf } from '@rask/ui/shell';
import type { Me } from '#lib/api/me.ts';
import { exact, seg, under } from '#lib/nav-match.ts';
import { isEstateAdmin } from '#lib/estate-admin.ts';

/**
 * The console's one navigation: every route, in one sidebar, on every page.
 *
 * Who may do what is the catalog's: each route's own guard and each door refuse what the caller may
 * not do. What this decides is only what the rail SHOWS, and it shows a row the caller may not open
 * as disabled with the denial's wording, never hidden: a missing row reads as a product without the
 * feature, while a disabled one names the relation to ask for.
 */

/** The catalog, in hierarchy order below a project: warehouse, namespace, table; then the stores
 *  and the held promotions (a decision gated on `can_promote` on the held table). */
const CATALOG: PageNavLeaf[] = [
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
	{ title: 'Tables', href: '/catalog/tables', match: seg('/catalog/tables'), icon: Database },
	{ title: 'Storage', href: '/catalog/storage', match: seg('/catalog/storage'), icon: HardDrive },
	{
		title: 'Promotions',
		href: '/catalog/promotions',
		match: seg('/catalog/promotions'),
		icon: ShieldCheck,
	},
];

/** Running the estate: its events, streams and dead letters, behind the estate-admin door. */
const OPERATIONS: PageNavLeaf[] = [
	{ title: 'Events', href: '/admin/events', match: seg('/admin/events'), icon: Radio },
	{ title: 'Streams', href: '/admin/streams', match: seg('/admin/streams'), icon: Layers },
	{ title: 'DLQ', href: '/admin/dlq', match: seg('/admin/dlq'), icon: Inbox },
];

/** Platform configuration, read across every project: who may do what, and the audit trail. */
const SETTINGS: PageNavLeaf = {
	title: 'Settings',
	href: '/settings',
	match: under('/settings'),
	icon: Settings,
	children: [
		{ title: 'Users & roles', href: '/settings/access', match: seg('/settings/access') },
		{ title: 'Audit', href: '/settings/audit', match: seg('/settings/audit') },
	],
};

/** A row the caller may not open: shown, disabled with `reason`, and without its sub-routes, so
 *  showing it hands out none of them. */
function refused(leaf: PageNavLeaf, reason: string): PageNavLeaf {
	return {
		title: leaf.title,
		href: leaf.href,
		match: leaf.match,
		icon: leaf.icon,
		unavailable: reason,
	};
}

/** The first path segment — `admin`, `catalog`, `workbench`, … — which decides the canvas layout. */
export function areaOf(pathname: string): string {
	return pathname.split('/').filter(Boolean)[0] ?? '';
}

/**
 * The sidebar for `me`, the resolved `/management/v1/me` (null: signed out or unresolved). An estate
 * admin gets every row live; anyone else gets the estate-admin rows disabled with the reason.
 */
export function consoleSidebar(me: Me | null): PageNav {
	const admin = isEstateAdmin(me);
	// The FGA denial's own wording, so the rail, the route's 403 and the API name the same relation on
	// the same object. An unresolved identity is told to sign in instead: naming a relation to someone
	// the console cannot identify is no advice.
	const denial = me
		? 'estate-admin only (needs can_observe_events on the FGA root)'
		: 'sign in to see whether you can open this';
	return {
		groups: [
			{
				items: [
					{ title: 'Home', href: '/', match: exact('/'), icon: House },
					{ title: 'Projects', href: '/projects', match: under('/projects'), icon: FolderKanban },
				],
			},
			{ label: 'Catalog', items: CATALOG },
			{
				label: 'Operations',
				items: admin ? OPERATIONS : OPERATIONS.map((leaf) => refused(leaf, denial)),
			},
		],
		footer: {
			items: [
				{ title: 'Workbench', href: '/workbench', match: seg('/workbench'), icon: LayoutDashboard },
				admin ? SETTINGS : refused(SETTINGS, denial),
			],
		},
	};
}
