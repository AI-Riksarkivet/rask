import { FolderKanban } from '@lucide/svelte';
import { exact, type ZoneNav } from '@rask/ui/shell';

// The home zone's OWN sidebar routes — the landing IS the project gallery, so one leaf. Home owns
// the origin root ('/'), matched exactly (every other path belongs to another zone).
export const HOME_ZONE_NAV: ZoneNav = {
	title: 'Home',
	leaves: [{ title: 'Projects', href: '/', match: exact('/'), icon: FolderKanban }],
};
