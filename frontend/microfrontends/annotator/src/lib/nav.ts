import { FolderKanban, Images } from '@lucide/svelte';
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
			label: 'Data',
			items: [
				{
					title: 'Browse corpus',
					href: '/annotator/browse',
					match: seg('/annotator/browse'),
					icon: Images,
				},
			],
		},
	],
};
