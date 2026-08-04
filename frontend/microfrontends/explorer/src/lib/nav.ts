import { FolderTree, LayoutDashboard, Map, Search, Share2, Workflow } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';
import type { DatasetView } from '@rask/explorer-api/descriptor';

// The explorer zone's OWN sidebar routes (the shared shell renders exactly what a zone passes — the
// cross-zone list lives in the top navbar). Hrefs are absolute domain paths: the zone is served
// under its `/explorer` base both standalone (dev/e2e) and behind the ingress. Search sits at the
// zone root, so it matches EXACTLY (`seg` would light it up on every sibling). Every leaf is
// same-zone, so they all stay soft navs — the annotator is reached from the top navbar, not from
// this sidebar.

/**
 * What the ACTIVE DATASET must declare for each area to be reachable.
 *
 * This is the whole point of the file being a function. The nav used to be a static const listing
 * Search/Atlas/Tree/Graph unconditionally, so a corpus that declares no embedding spaces still got
 * an Atlas link and one with no knowledge graph still got Graph. The seeded `transcripts_v2`
 * declares `atlas: []` and `capabilities: {frames}` — and all four rendered anyway. Clicking one led
 * to a page that could only report that the thing it exists to show does not exist here, which
 * reads as a broken product rather than an inapplicable view.
 *
 * The gates use capability names the SERVICE already reads, so a declaration cannot mean one thing
 * to the sidebar and another to the endpoint behind it: `topics` is what `topics.py` looks up to
 * find the hierarchy table, and `graph` is what `graph.py` looks up for the KG. Atlas gates on the
 * declared spaces themselves rather than a capability, because that list is what the atlas page
 * already consults to decide what it can draw.
 */
const AREAS = [
	{
		title: 'Search',
		href: '/explorer',
		match: exact('/explorer'),
		icon: Search,
		// Search is what a dataset IS — the row table is required by the descriptor schema, so there
		// is no corpus with nothing to search. `always` rather than `available: () => true` because
		// this is also the set that renders BEFORE any descriptor exists, and that needs to be a fact
		// about the entry rather than something inferred from which function it happens to hold.
		always: true,
	},
	{
		title: 'Atlas',
		href: '/explorer/atlas',
		match: seg('/explorer/atlas'),
		icon: Map,
		// Table-SENSITIVE, and the only gate that is. An atlas space is bound to a specific table
		// (`AtlasSpace.table`), so a corpus with an atlas over `pages` has nothing to draw while you
		// are searching `lines`. Falls back to "any space" when no table is active, which is the
		// single-table corpus every descriptor on disk still describes.
		available: (view: DatasetView, rowTable: string | null) =>
			rowTable === null
				? view.atlasSpaces.length > 0
				: view.atlasSpaces.some((space) => space.table === rowTable),
	},
	{
		title: 'Tree',
		href: '/explorer/tree',
		match: seg('/explorer/tree'),
		icon: FolderTree,
		// The SCHEMA, not the runtime. `/tree` gated on `/api/topics` answering `res.built`, which
		// conflates "this corpus has no hierarchy" with "the hierarchy has not been computed yet" —
		// two different answers, and only the first is a reason to hide the door.
		// Dataset-level, not table-level: `capabilities` is declared once per corpus, so switching
		// tables cannot change it. Stated rather than left implicit — the asymmetry with Atlas above
		// is a property of the descriptor, not an oversight here.
		available: (view: DatasetView) => view.hasCapability('topics'),
	},
	{
		title: 'Graph',
		href: '/explorer/graph',
		match: seg('/explorer/graph'),
		icon: Share2,
		// `/graph` consulted the descriptor NOWHERE — zero references — so it was offered on every
		// corpus in the estate regardless of whether one had ever been built.
		available: (view: DatasetView) => view.hasCapability('graph'),
	},
] as const;

/**
 * This zone's sidebar for one dataset.
 *
 * `view` is null until the descriptor lands. Everything conditional is then ABSENT rather than
 * shown-and-broken: the estate's posture everywhere else (Settings is absent for a non-admin, not
 * disabled), and the honest one here — the layout already withholds the page itself until the
 * descriptor resolves, so a rail that briefly shows only what is certain matches the page beside it.
 *
 * Called from a `$derived` in the layout, so switching datasets recomputes the rail: a corpus with an
 * atlas and one without must not share a sidebar just because they were visited in the same session.
 */
export function explorerZoneNav(
	view: DatasetView | null,
	activeRowTable: string | null = null,
): ZoneNav {
	const explore = AREAS.filter(
		(area) =>
			('always' in area && area.always) ||
			(view !== null && 'available' in area && area.available(view, activeRowTable)),
	).map(({ title, href, match, icon }) => ({ title, href, match, icon }));

	return {
		title: 'Explorer',
		groups: [
			{ label: 'Explore', items: explore },
			{
				label: 'Workspace',
				items: [
					{
						title: 'Workflow',
						href: '/explorer/workflow',
						match: seg('/explorer/workflow'),
						icon: Workflow,
					},
				],
			},
			// The "Help" group and its single `Guide` row are GONE. A sidebar is where a zone's areas
			// live, and documentation is not an area of the corpus — it sat beside Search and Atlas
			// implying it was another way to look at the data.
		],
		// PINNED to the rail's bottom. The dock is not one of this zone's AREAS — it is where you go to
		// work across them — so it sits below the areas rather than scrolling among them.
		footer: {
			label: 'Workspace',
			items: [
				{
					title: 'Workbench',
					href: '/explorer/workbench',
					match: seg('/explorer/workbench'),
					icon: LayoutDashboard,
				},
			],
		},
	};
}
