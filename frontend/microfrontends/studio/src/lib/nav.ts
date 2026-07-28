import { LayoutGrid, Sparkles } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The studio zone's OWN sidebar routes. Like the annotator, this zone passed no zoneNav and so
// rendered no rail — inconsistent with every other zone for no reason other than that nobody had
// written the config. The launcher sits at the zone root, so it matches EXACTLY (`seg` would light
// it up on the animation route too).
export const STUDIO_ZONE_NAV: ZoneNav = {
	title: 'Studio',
	groups: [
		{
			label: 'Studio',
			items: [
				{ title: 'Apps', href: '/studio', match: exact('/studio'), icon: LayoutGrid },
				{
					title: 'Animation',
					href: '/studio/animation',
					match: seg('/studio/animation'),
					icon: Sparkles,
				},
			],
		},
	],
};
