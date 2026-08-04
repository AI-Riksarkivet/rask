import {
	BookOpen,
	FolderTree,
	LayoutDashboard,
	Map,
	Search,
	Share2,
	Workflow,
} from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The explorer zone's OWN sidebar routes (the shared shell renders exactly what a zone passes — the
// cross-zone list lives in the top navbar). Hrefs are absolute domain paths: the zone is served
// under its `/explorer` base both standalone (dev/e2e) and behind the ingress. Search sits at the
// zone root, so it matches EXACTLY (`seg` would light it up on every sibling). Every leaf is
// same-zone, so they all stay soft navs — the annotator is reached from the top navbar, not from
// this sidebar.
//
// Grouped by what you are doing: finding things, working on them, and reading about it.
export const EXPLORER_ZONE_NAV: ZoneNav = {
	title: 'Explorer',
	groups: [
		{
			label: 'Explore',
			items: [
				{ title: 'Search', href: '/explorer', match: exact('/explorer'), icon: Search },
				{ title: 'Atlas', href: '/explorer/atlas', match: seg('/explorer/atlas'), icon: Map },
				{ title: 'Tree', href: '/explorer/tree', match: seg('/explorer/tree'), icon: FolderTree },
				{ title: 'Graph', href: '/explorer/graph', match: seg('/explorer/graph'), icon: Share2 },
			],
		},
		{
			label: 'Workspace',
			items: [
				{
					title: 'Workflow',
					href: '/explorer/workflow',
					match: seg('/explorer/workflow'),
					icon: Workflow,
				},
			],
		},
		{
			label: 'Help',
			items: [
				{ title: 'Guide', href: '/explorer/guide', match: seg('/explorer/guide'), icon: BookOpen },
			],
		},
	],
	// PINNED to the rail's bottom. The dock is not one of this zone's AREAS — it is where you go to
	// work across them — so it sits below the areas rather than scrolling among them.
	footer: {
		label: 'Workspace',
		items: [
			{
				title: 'Workbench',
				href: '/explorer/workbench',
				match: seg('/explorer/workbench'),
				icon: LayoutDashboard,
			},
		],
	},
};
