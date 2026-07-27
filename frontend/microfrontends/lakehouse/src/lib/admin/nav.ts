import { Building2, Inbox, Layers, Radio, ScrollText, ShieldCheck } from '@lucide/svelte';
import { seg, type ZoneNav } from '@rask/ui/shell';

// The admin area's sidebar routes inside the lakehouse zone. Access is the estate access-review surface (playground +
// grants) — it also has its own top-navbar entry, mirrored here so the sidebar is complete.
export const ADMIN_ZONE_NAV: ZoneNav = {
	title: 'Admin',
	leaves: [
		{
			title: 'Tenants',
			href: '/lakehouse/admin/tenants',
			match: seg('/lakehouse/admin/tenants'),
			icon: Building2,
		},
		{
			title: 'Audit',
			href: '/lakehouse/admin/audit',
			match: seg('/lakehouse/admin/audit'),
			icon: ScrollText,
		},
		{
			title: 'Streams',
			href: '/lakehouse/admin/streams',
			match: seg('/lakehouse/admin/streams'),
			icon: Layers,
		},
		{ title: 'DLQ', href: '/lakehouse/admin/dlq', match: seg('/lakehouse/admin/dlq'), icon: Inbox },
		{
			title: 'Events',
			href: '/lakehouse/admin/events',
			match: seg('/lakehouse/admin/events'),
			icon: Radio,
		},
		{
			title: 'Access',
			href: '/lakehouse/admin/access',
			match: seg('/lakehouse/admin/access'),
			icon: ShieldCheck,
		},
	],
};
