import { Bot, FolderKanban, Layers, Scale, SlidersHorizontal } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The annotate zone's OWN sidebar routes.
//
// This zone shipped `zoneNav={null}` for a while, on the argument that its two rows read as a second
// sidebar beside the annotate view's annotation panel. That argument was about the CANVAS, and it
// cost far more than it claimed: `AppShell` gates the WHOLE `<AppSidebar>` on
// `zoneNavLeaves(zoneNav).length > 1`, and the sidebar header is where the PROJECT SWITCHER lives.
// So passing null did not remove two rows — it removed the project dropdown, the estate zone links
// and the collapse trigger, and landing in Annotate from anywhere else silently stripped away
// navigation every other zone provides. That is the exact failure `app-shell.svelte` describes
// having already fixed for `canvas` mode; the null path reintroduced it by another door.
//
// The canvas concern is real and is now handled where it belongs: a `canvas` zone starts the rail
// ICON-COLLAPSED (see `sidebarOpen` in app-shell.svelte), so the drawing surface keeps its width and
// the rail is one click away instead of gone.
//
// THE ROWS. Reported: "why is there labeling tasks and group data browse-corpus? and not labeling
// tasks and bulk labeling, and LLM-as-a-judge, AI-assistance, Annotation settings". The old rail
// named its rows after the SCREENS that happened to exist ("Data" → "Browse corpus") rather than
// after the jobs this zone does — so the rail described the implementation, and the jobs that were
// not yet screens were invisible, indistinguishable from a product that does not do them.
//
// `/annotator/browse` keeps its path: it IS still the corpus browser, and it is where a bulk send
// starts (#42). Only the ROW is renamed, because "browse corpus" names the mechanism and "Bulk
// labeling" names the job — and the job is why anyone opens it.
//
// Hrefs are absolute domain paths: the zone is served under its `/annotator` base both standalone
// (dev/e2e) and behind the ingress. Every leaf is same-zone, so they all stay soft navs (no
// `reload`).
export const ANNOTATOR_ZONE_NAV: ZoneNav = {
	title: 'Annotate',
	// Labeling tasks is the ZONE ROOT — it is what `/annotator` renders. `exact`, not `seg`: `seg`
	// would light it up on `/annotator/browse` and on every project detail page too, which is the
	// bug #29 fixed in the compute rail.
	root: {
		title: 'Labeling tasks',
		href: '/annotator',
		match: exact('/annotator'),
		icon: FolderKanban,
	},
	groups: [
		{
			// Two ways work reaches a labeler: one item at a time from a queue (the root), or in bulk
			// by selecting a slice of a corpus and sending it.
			label: 'Label',
			items: [
				{
					title: 'Bulk labeling',
					href: '/annotator/browse',
					match: seg('/annotator/browse'),
					icon: Layers,
				},
			],
		},
		{
			// Everything that judges or produces labels WITHOUT a person drawing them. Its own group
			// because the blast radius differs: a bad model here quietly degrades every task in the
			// zone, where a bad manual label degrades one item.
			label: 'Automation',
			items: [
				{
					title: 'LLM-as-a-judge',
					href: '/annotator/judge',
					match: seg('/annotator/judge'),
					icon: Scale,
				},
				{
					title: 'AI assistance',
					href: '/annotator/assist',
					match: seg('/annotator/assist'),
					icon: Bot,
				},
			],
		},
		{
			label: 'Configure',
			items: [
				{
					title: 'Annotation settings',
					href: '/annotator/settings',
					match: seg('/annotator/settings'),
					icon: SlidersHorizontal,
				},
			],
		},
	],
};
