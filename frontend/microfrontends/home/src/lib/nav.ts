import { FolderKanban } from '@lucide/svelte';
import { type ZoneNav, under } from '@rask/ui/shell';

// The home zone's OWN sidebar routes — ONE leaf, and it is Projects.
//
// It points at `/projects`, NOT at '/'. Both render the same gallery, but only one of them is the
// addressable projects surface: `/projects` is what the navbar's own Projects entry targets
// (2026-08-03 ruling — a project is the TOP of the hierarchy, so it leads the main menu), and a leaf
// labelled "Projects" that resolved somewhere else was the sidebar disagreeing with the bar above it
// — the same class of drift that had the lakehouse sidebar saying "Admin" while its panel said
// "Operations". `under` rather than `exact`, so the leaf stays lit on `/projects/<project>`.
//
// One leaf is why home renders NO rail: the shell counts leaves across every group and suppresses
// the sidebar entirely below two, because a rail whose only row links to the page you are already
// on costs width and implies areas that do not exist.
export const HOME_ZONE_NAV: ZoneNav = {
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
