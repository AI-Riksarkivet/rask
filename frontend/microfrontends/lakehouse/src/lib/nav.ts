import { base } from '$app/paths';
import {
	Activity,
	Boxes,
	Columns3,
	Cpu,
	Database,
	FlaskConical,
	Gauge,
	HardDrive,
	LayoutDashboard,
	Inbox,
	Layers,
	Network,
	Package,
	Radio,
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
 * Governance is NOT here. Who-may-do-what (access, audit) is a different question from operating the
 * estate (streams, events, DLQ) — merging the two is what turned "Admin" into a junk drawer — and it
 * is also a different LEVEL: those surfaces read across every project and take none, so #105 re-based
 * them under the home zone's `/settings/`. A zone rail names the zone's own routes.
 */
/** The zone ROOT row — first leaf in the rail, above and outside every group (#109).
 *
 *  `/lakehouse` used to 307 into the catalog, so the rail's first entry was an AREA and the zone had
 *  no landing at all. It has one now: the active project's overview — its hierarchy and the
 *  warehouses claiming it. That is not a member of Catalog, Lineage, Models or Operations; it
 *  summarises what all of them hang beneath, which is exactly what `ZoneNav.root` exists for (the
 *  compute zone's Overview is the precedent, and it was moved out of "Cluster" for the same reason).
 *
 *  `exact`, not `seg`: `seg('/lakehouse')` matches every route in the zone and would light the
 *  landing up from inside every area. */
const LAKEHOUSE_ROOT: ZoneNav['root'] = {
	title: 'Overview',
	href: '/lakehouse',
	match: exact('/lakehouse'),
	icon: Gauge,
};

const LAKEHOUSE_GROUPS: ZoneNav['groups'] = [
	{
		label: 'Catalog',
		// NO 'Projects' LEAF, by ruling (2026-08-03) — the same one that took the tenants row out of
		// the shared navbar's Lakehouse panel. A project is the TOP of the hierarchy (project ›
		// warehouse › namespace › table), so listing "projects" as a leaf INSIDE one project's catalog
		// inverted it: it described lakekeeper's tenant-list endpoint rather than this product's model.
		// There is ONE project concept and it belongs to the main menu: the list, the per-project
		// overview and the create flow are the HOME zone's `/projects` and `/projects/<project>`.
		// `/lakehouse/catalog/projects` does not resolve at all any more — this zone owns what is BELOW
		// a project (warehouses › namespaces › tables), and nothing above it.
		items: [
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
		// OPERATIONS, not "Admin", and without Tenants — by ruling (2026-08-03). The shared navbar's
		// panel has said Governance = [Access, Tenants, Audit] and Operations = [Events, Streams, DLQ]
		// for some time; this sidebar still said "Admin" and kept Tenants inside it, so the same estate
		// answered the same question two different ways depending on which control you opened. Who may
		// do what — access, TENANTS, audit — is governance; running the estate — events, streams, dead
		// letters — is operations. The navbar was right, so the sidebar moves to it.
		//
		// The GOVERNANCE group that sat above this one is GONE (#105): Access and Audit are estate
		// surfaces, not lakehouse ones — they read across every project and neither takes one — so they
		// were re-based under the home zone's `/settings/`, and Tenants was retired outright because
		// `/projects` already IS the estate's project list. This rail is a ZONE rail: a leaf pointing
		// into another zone would be the sidebar advertising a hop the top navbar owns.
		label: 'Operations',
		// The operational drawer — real, but not what anyone opens the lakehouse for. Collapsed until
		// you are actually inside it (the shell auto-expands whichever group holds the active route).
		items: [
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

/** PINNED to the rail's bottom, outside `groups`. The dock briefly sat as the last leaf of
 *  "Lineage", which made a ZONE-level surface read as a lineage feature and hid it from anyone
 *  standing in catalog or models. It composes the whole zone — graph + runs + events over ONE
 *  LineageState, plus the catalog's own table registry and object browser — so it belongs below the
 *  areas rather than among them. Not privilege-filtered: every identity that can see the zone can
 *  open its dock. */
const LAKEHOUSE_FOOTER: ZoneNav['footer'] = {
	label: 'Workspace',
	items: [
		{
			title: 'Workbench',
			href: '/lakehouse/workbench',
			match: seg('/lakehouse/workbench'),
			icon: LayoutDashboard,
		},
	],
};

/** Groups behind the estate-admin door — the ones `/lakehouse/admin/*` serves.
 *
 *  The label has to MATCH the group's own `label`, and it did not: the set said `'Admin'` while the
 *  group renamed itself to `'Operations'`, so the filter matched nothing on that side and the
 *  operational drawer leaked to every identity in the sidebar (the navbar half was always correct —
 *  `nav-config.ts` gates its Operations column on `estateAdmin`). It was masked while `'Governance'`
 *  still matched something; #105 took that group away, which would have left this set filtering
 *  literally nothing. Fixed with the group it belongs to. */
const PRIVILEGED_GROUPS = new Set(['Operations']);

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
		root: LAKEHOUSE_ROOT,
		groups: estateAdmin
			? LAKEHOUSE_GROUPS
			: LAKEHOUSE_GROUPS.filter((g) => !PRIVILEGED_GROUPS.has(g.label)),
		footer: LAKEHOUSE_FOOTER,
	};
}
