import { Boxes, Database, FolderKanban, Warehouse } from '@lucide/svelte';
import { seg, type ZoneNav } from '@rask/ui/shell';

// The data area's sidebar routes inside the lakehouse zone (the shared shell renders exactly what a zone passes — the
// cross-zone list lives in the top navbar). Hrefs are absolute domain paths: the zone is served
// under its `/data` base both standalone (dev/e2e) and behind the ingress, so `/data/...` is
// correct everywhere, and `seg` keeps a leaf lit across its nested detail pages.
export const DATA_ZONE_NAV: ZoneNav = {
	title: 'Data',
	leaves: [
		{
			title: 'Projects',
			href: '/lakehouse/data/projects',
			match: seg('/lakehouse/data/projects'),
			icon: FolderKanban,
		},
		{
			title: 'Tables',
			href: '/lakehouse/data/tables',
			match: seg('/lakehouse/data/tables'),
			icon: Database,
		},
		{
			title: 'Namespaces',
			href: '/lakehouse/data/namespaces',
			match: seg('/lakehouse/data/namespaces'),
			icon: Boxes,
		},
		{
			title: 'Warehouses',
			href: '/lakehouse/data/warehouses',
			match: seg('/lakehouse/data/warehouses'),
			icon: Warehouse,
		},
	],
};
