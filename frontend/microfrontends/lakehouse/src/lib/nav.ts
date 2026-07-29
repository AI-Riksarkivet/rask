import { base } from '$app/paths';
import {
	Activity,
	Boxes,
	Building2,
	Columns3,
	Cpu,
	Database,
	FlaskConical,
	FolderKanban,
	HardDrive,
	Inbox,
	Layers,
	LayoutDashboard,
	Network,
	Package,
	Radio,
	ScrollText,
	ShieldCheck,
	Warehouse,
	Workflow,
} from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

/**
 * The lakehouse zone's WHOLE surface, in one grouped sidebar.
 *
 * This used to be five separate `ZoneNav`s — data, lineage, models, admin, storage — chosen by a
 * function that swapped the ENTIRE sidebar based on which area you happened to be standing in. That
 * was a leftover from when those areas were five separate SvelteKit apps: the flat `ZoneNav` could
 * express only one unlabelled list, so showing every area at once would have meant showing no labels
 * at all.
 *
 * The cost was not cosmetic. Standing in the catalog you could not see that lineage, models,
 * governance or admin existed, and the object browser at /lakehouse/catalog/storage was reachable ONLY by
 * typing its URL, because no area's list was permitted to name another area's route. Grouping is
 * exactly what the shadcn sidebar primitives (Group / GroupLabel / MenuSub) exist for, and they were
 * already vendored in @rask/ui — merely never used for structure.
 *
 * Governance is split out of admin deliberately: who-may-do-what (access, audit) is a different
 * question from operating the estate (tenants, streams, events, DLQ). Merging the two is what turned
 * "Admin" into a junk drawer.
 */
const LAKEHOUSE_GROUPS: ZoneNav['groups'] = [
	{
		label: 'Catalog',
		items: [
			{
				title: 'Projects',
				href: '/lakehouse/catalog/projects',
				match: seg('/lakehouse/catalog/projects'),
				icon: FolderKanban,
			},
			{
				title: 'Namespaces',
				href: '/lakehouse/catalog/namespaces',
				match: seg('/lakehouse/catalog/namespaces'),
				icon: Boxes,
			},
			{
				title: 'Tables',
				href: '/lakehouse/catalog/tables',
				match: seg('/lakehouse/catalog/tables'),
				icon: Database,
			},
			{
				title: 'Warehouses',
				href: '/lakehouse/catalog/warehouses',
				match: seg('/lakehouse/catalog/warehouses'),
				icon: Warehouse,
			},
			{
				// R28: the object browser belongs WITH the catalog that governs it, not beside it as a
				// sibling area. Being neither is precisely why nothing linked to it.
				title: 'Storage',
				href: '/lakehouse/catalog/storage',
				match: seg('/lakehouse/catalog/storage'),
				icon: HardDrive,
				// "Stores by tier" used to hang here as a CHILD. It is not a separate destination — it
				// is a second lens on the registry the browser already lists from ("which store backs
				// silver?"), so it now lives as a TAB inside the storage view itself
				// (`catalog/storage/+layout.svelte`, route `catalog/storage/tiers`). A nested rail entry
				// made two views of one subject read as two areas, and hung a second-level item beside
				// top-level ones. The old `/catalog/stores` URL 308s to the tab.
			},
		],
	},
	{
		label: 'Lineage',
		items: [
			{
				title: 'Graph',
				href: '/lakehouse/lineage',
				match: exact('/lakehouse/lineage'),
				icon: Network,
			},
			{
				title: 'Datasets',
				href: '/lakehouse/lineage/datasets',
				match: seg('/lakehouse/lineage/datasets'),
				icon: Boxes,
			},
			{
				title: 'Jobs',
				href: '/lakehouse/lineage/jobs',
				match: seg('/lakehouse/lineage/jobs'),
				icon: Cpu,
			},
			{
				title: 'Runs',
				href: '/lakehouse/lineage/runs',
				match: seg('/lakehouse/lineage/runs'),
				icon: Activity,
			},
			{
				title: 'Columns',
				href: '/lakehouse/lineage/columns',
				match: seg('/lakehouse/lineage/columns'),
				icon: Columns3,
			},
			{
				title: 'Workbench',
				href: '/lakehouse/lineage/workbench',
				match: seg('/lakehouse/lineage/workbench'),
				icon: LayoutDashboard,
			},
		],
	},
	{
		label: 'Models',
		items: [
			{
				title: 'Registry',
				href: '/lakehouse/models',
				match: exact('/lakehouse/models'),
				icon: Package,
			},
			{
				title: 'Pipeline',
				href: '/lakehouse/models/pipeline',
				match: seg('/lakehouse/models/pipeline'),
				icon: Workflow,
			},
			{
				title: 'Experiments',
				href: '/lakehouse/models/experiments',
				match: seg('/lakehouse/models/experiments'),
				icon: FlaskConical,
			},
		],
	},
	{
		label: 'Governance',
		items: [
			{
				title: 'Access',
				href: '/lakehouse/governance/access',
				match: seg('/lakehouse/governance/access'),
				icon: ShieldCheck,
			},
			{
				title: 'Audit',
				href: '/lakehouse/governance/audit',
				match: seg('/lakehouse/governance/audit'),
				icon: ScrollText,
			},
		],
	},
	{
		label: 'Admin',
		// The operational drawer — real, but not what anyone opens the lakehouse for. Collapsed until
		// you are actually inside it (the shell auto-expands whichever group holds the active route).
		defaultCollapsed: true,
		items: [
			{
				title: 'Tenants',
				href: '/lakehouse/admin/tenants',
				match: seg('/lakehouse/admin/tenants'),
				icon: Building2,
			},
			{
				title: 'Streams',
				href: '/lakehouse/admin/streams',
				match: seg('/lakehouse/admin/streams'),
				icon: Layers,
			},
			{
				title: 'Events',
				href: '/lakehouse/admin/events',
				match: seg('/lakehouse/admin/events'),
				icon: Radio,
			},
			{
				title: 'DLQ',
				href: '/lakehouse/admin/dlq',
				match: seg('/lakehouse/admin/dlq'),
				icon: Inbox,
			},
		],
	},
];

/** Groups behind the estate-admin door — the ones `/lakehouse/admin/*` serves. */
const PRIVILEGED_GROUPS = new Set(['Governance', 'Admin']);

/** The area segment right after this zone's base — `''` on the zone root. Still load-bearing: the
 *  root layout gates the admin door on it and sizes the lineage canvas with it. */
export function areaOf(pathname: string): string {
	const rest = pathname.startsWith(base) ? pathname.slice(base.length) : pathname;
	return rest.split('/').filter(Boolean)[0] ?? '';
}

/**
 * The zone's sidebar, minus anything the caller may not use.
 *
 * The door used to hide the sidebar ENTIRELY (`zoneNav = null`) for a non-admin on an admin route.
 * That was tolerable when the sidebar only ever showed one area; with one merged nav it would blank
 * the catalog, lineage and models rows too — punishing a user for visiting a URL they were denied.
 * Privilege is now a per-GROUP filter, so a denied identity keeps the navigation it is entitled to
 * and simply never sees Governance or Admin. The door itself is unchanged and still fail-closed;
 * this only stops advertising routes it would refuse.
 */
export function lakehouseSidebar(estateAdmin: boolean): ZoneNav {
	return {
		title: 'Lakehouse',
		groups: estateAdmin
			? LAKEHOUSE_GROUPS
			: LAKEHOUSE_GROUPS.filter((g) => !PRIVILEGED_GROUPS.has(g.label)),
	};
}
