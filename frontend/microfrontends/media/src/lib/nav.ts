import {
	BookOpen,
	FolderTree,
	LayoutDashboard,
	Map,
	Search,
	Share2,
	SquarePen,
	Workflow,
} from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The media zone's OWN sidebar routes (the shared shell renders exactly what a zone passes — the
// cross-zone list lives in the top navbar). Hrefs are absolute domain paths: the zone is served
// under its `/media` base both standalone (dev/e2e) and behind the ingress. Search sits at the
// zone root, so it matches EXACTLY (`seg` would light it up on every sibling). Annotate is the
// one CROSS-ZONE leaf — a literal /annotator path outside this app's route manifest — so it
// declares `reload` and the shell hard-navigates it.
//
// Grouped by what you are doing: finding things, working on them, and reading about it.
export const MEDIA_ZONE_NAV: ZoneNav = {
	title: 'Search',
	groups: [
		{
			label: 'Explore',
			items: [
				{ title: 'Search', href: '/media', match: exact('/media'), icon: Search },
				{ title: 'Atlas', href: '/media/atlas', match: seg('/media/atlas'), icon: Map },
				{ title: 'Tree', href: '/media/tree', match: seg('/media/tree'), icon: FolderTree },
				{ title: 'Graph', href: '/media/graph', match: seg('/media/graph'), icon: Share2 },
			],
		},
		{
			label: 'Workspace',
			items: [
				{
					title: 'Workbench',
					href: '/media/workbench',
					match: seg('/media/workbench'),
					icon: LayoutDashboard,
				},
				{
					title: 'Workflow',
					href: '/media/workflow',
					match: seg('/media/workflow'),
					icon: Workflow,
				},
				{
					title: 'Annotate',
					href: '/annotator/',
					match: seg('/annotator'),
					icon: SquarePen,
					reload: true,
				},
			],
		},
		{
			label: 'Help',
			items: [{ title: 'Guide', href: '/media/guide', match: seg('/media/guide'), icon: BookOpen }],
		},
	],
};
