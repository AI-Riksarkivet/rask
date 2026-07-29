import { FolderKanban } from '@lucide/svelte';
import { exact, type ZoneNav } from '@rask/ui/shell';

// The home zone's OWN sidebar routes — the landing IS the project gallery, so ONE leaf. Home owns
// the origin root ('/'), matched exactly (every other path belongs to another zone).
//
// One leaf is why home renders NO rail: the shell counts leaves across every group and suppresses
// the sidebar entirely below two, because a rail whose only row links to the page you are already
// on costs width and implies areas that do not exist.
export const HOME_ZONE_NAV: ZoneNav = {
	title: 'Home',
	groups: [
		{
			label: 'Home',
			items: [{ title: 'Projects', href: '/', match: exact('/'), icon: FolderKanban }],
		},
	],
};
