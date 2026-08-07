import { Activity, ChartLine, FlaskConical, ListChecks, Package, Rocket } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

/**
 * The MODELS zone's sidebar — one model's whole life, in one rail.
 *
 * This zone began as `train` (submit / runs / monitoring / analysis, all placeholder) while the
 * governed registry lived in the lakehouse at `/lakehouse/models`. That split asked a reader to know
 * that "the models area" and "the training zone" were two different places, and it filed the registry
 * — the thing a training run PRODUCES — inside the zone that governs tables. The registry and its
 * experiments moved here; the lakehouse keeps model LINEAGE, which is a lineage question and belongs
 * with the graph.
 *
 * REGISTRY IS THE ZONE ROOT, matched with `exact` — it is what this zone is for, and `seg` would keep
 * it lit on every sibling area (the same reason nav-config's own Registry/Graph leaves use `exact`).
 * Training therefore moves off the root onto `/models/submit`, so every area is a named segment and
 * none of them is "whatever the root happens to be".
 *
 * The two groups, and why they are two:
 *
 *  - REGISTRY — what a model IS: its versions and the experiments behind them. These are the surfaces
 *    that physically migrated out of the lakehouse.
 *  - TRAINING — the four surfaces the `train` zone shipped, placeholder badges and all. They are
 *    honest scaffolding for a service that does not exist yet, and deleting them would lose the
 *    intended contract without replacing it.
 *
 * TWO groups, not the three this comment used to describe. INFERENCE went to `/compute/inference` at
 * #131 (running a version is a compute verb, not a registry one) and the `/models/pipeline` trigger
 * door was deleted 2026-08-07: it fired the medallion cascade head and a train request from a button,
 * which is an OPERATION on the lakehouse rather than anything about a model, and the cascade is
 * event-driven — the button was a second, manual writer to a plane that already has one.
 *
 * KEEP THE RATIONALE UP HERE, not between the leaves. `nav-truth.test.ts` scans this file with a
 * regex that pairs each `title:` with an `href:` within 200 characters and then looks ahead to the
 * next `title:` — so a long comment sitting BETWEEN two leaves pushes them out of the window and the
 * gate silently stops checking those hrefs. The failure is invisible: the suite stays green while
 * covering less. (Caught here: two leaves dropped out of the scan, taking the estate's leaf count
 * from 32 to 30 and tripping the scanner's own `> 30` guard.)
 */
export const MODELS_ZONE_NAV: ZoneNav = {
	title: 'Models',
	groups: [
		{
			label: 'Registry',
			items: [
				{ title: 'Registry', href: '/models', match: exact('/models'), icon: Package },
				{
					title: 'Experiments',
					href: '/models/experiments',
					match: seg('/models/experiments'),
					icon: FlaskConical,
				},
			],
		},
		{
			label: 'Training',
			items: [
				{ title: 'Submit', href: '/models/submit', match: seg('/models/submit'), icon: Rocket },
				{ title: 'Runs', href: '/models/runs', match: seg('/models/runs'), icon: ListChecks },
				{
					title: 'Monitoring',
					href: '/models/monitoring',
					match: seg('/models/monitoring'),
					icon: Activity,
				},
				{
					title: 'Analysis',
					href: '/models/analysis',
					match: seg('/models/analysis'),
					icon: ChartLine,
				},
			],
		},
	],
};
