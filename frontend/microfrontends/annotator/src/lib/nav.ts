import { FolderKanban, Library } from '@lucide/svelte';
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
			items: [
				// The landing IS the project list (S9); the canvas opens from a claimed task (or a
				// `?keys=` deep link), so it needs no leaf of its own.
				{
					title: 'Labeling tasks',
					href: '/annotator',
					match: exact('/annotator'),
					icon: FolderKanban,
				},
				{
					title: 'Browse corpus',
					href: '/annotator/browse',
					match: seg('/annotator/browse'),
					icon: Library,
				},
			],
		},
	],
};

// The Corpus group (Search / Atlas / Graph cross-zone links) is GONE on purpose: the flow runs the
// other way — you select data points IN media (search results, an atlas lasso) and SEND them into a
// project; the annotator's rail is the project workspace, not a launcher for the corpus tools.
// The top navbar still reaches every zone.
