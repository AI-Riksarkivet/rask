import { Map, PenLine, Search, Share2 } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The annotator's OWN sidebar. This zone had NO zoneNav at all — it was the single zone in the
// estate that rendered no rail, so landing here from Search stripped away the navigation every
// other zone provides and offered no way back except the top navbar.
//
// It owns exactly one route (the canvas), which is not enough on its own: the shell suppresses a
// rail below two leaves, precisely so a one-row sidebar linking to the page you are already on
// does not waste width. The corpus links earn the rail — they are the way BACK to the material you
// are annotating, which is the trip this zone actually needs. They leave this app's route manifest,
// so each declares `reload` and the shell hard-navigates (and prefetches on intent), the mirror of
// media's own Annotate leaf.
export const ANNOTATOR_ZONE_NAV: ZoneNav = {
	title: 'Annotate',
	groups: [
		{
			label: 'Annotate',
			items: [{ title: 'Canvas', href: '/annotator', match: exact('/annotator'), icon: PenLine }],
		},
		{
			label: 'Corpus',
			items: [
				{ title: 'Search', href: '/media/', match: seg('/media'), icon: Search, reload: true },
				{
					title: 'Atlas',
					href: '/media/atlas',
					match: seg('/media/atlas'),
					icon: Map,
					reload: true,
				},
				{
					title: 'Graph',
					href: '/media/graph',
					match: seg('/media/graph'),
					icon: Share2,
					reload: true,
				},
			],
		},
	],
};
