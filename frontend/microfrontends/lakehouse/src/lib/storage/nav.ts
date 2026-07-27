import { HardDrive } from '@lucide/svelte';
import { seg, type ZoneNav } from '@rask/ui/shell';

// The storage area's sidebar routes inside the lakehouse zone (R18: the S3 object browser over the
// warehouse buckets). Hrefs are absolute domain paths — the same contract as the sibling area navs.
export const STORAGE_ZONE_NAV: ZoneNav = {
	title: 'Storage',
	leaves: [
		{
			title: 'Objects',
			href: '/lakehouse/storage',
			match: seg('/lakehouse/storage'),
			icon: HardDrive,
		},
	],
};
