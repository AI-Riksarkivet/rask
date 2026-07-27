import { Activity, Boxes, Columns3, Cpu, Network } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The lineage area's sidebar routes inside the lakehouse zone (Marquez-parity IA): the four first-class views —
// Datasets / Jobs / Runs / Columns — plus the DAG explorer at the zone root. Hrefs are absolute
// domain paths (the zone is served under its `/lineage` base both standalone and behind the
// ingress); `seg` keeps a list leaf lit across its nested detail pages.
export const LINEAGE_ZONE_NAV: ZoneNav = {
	title: 'Lineage',
	leaves: [
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
			title: 'Graph',
			href: '/lakehouse/lineage',
			match: exact('/lakehouse/lineage'),
			icon: Network,
		},
	],
};
