import { LayoutDashboard, Workflow } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The workbench zone's own rail. Two leaves rather than one: the shell hides a rail below two, and a
// zone that silently renders no sidebar is the defect `zone-shell.test.ts` exists to catch.
//
// The SAVED VIEWS list is deliberately NOT here. It is page chrome — it changes as the user works —
// and `ZoneNav` is a static route table; putting a mutable list in it would mean the sidebar and the
// dock disagreed the moment a view was created.
export const WORKBENCH_ZONE_NAV: ZoneNav = {
	title: 'Workbench',
	groups: [
		{
			label: 'Workbench',
			items: [
				{ title: 'Dock', href: '/workbench/', match: exact('/workbench'), icon: LayoutDashboard },
				{
					title: 'Lineage graph',
					href: '/workbench/lineage',
					match: seg('/workbench/lineage'),
					icon: Workflow,
				},
			],
		},
	],
};
