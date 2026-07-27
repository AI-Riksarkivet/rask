import { Activity, Boxes, ChartLine, ListChecks, Rocket } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The train zone's OWN sidebar routes — the five areas R17 names: submit training,
// watch training runs, monitoring, analysis, and the model registry (viewing +
// testing models). The root leaf (Submit) matches exactly, or it would light up on
// every sibling area (see nav-config's Registry/Graph precedent).
export const TRAIN_ZONE_NAV: ZoneNav = {
	title: 'Train',
	leaves: [
		{ title: 'Submit', href: '/train', match: exact('/train'), icon: Rocket },
		{ title: 'Runs', href: '/train/runs', match: seg('/train/runs'), icon: ListChecks },
		{
			title: 'Monitoring',
			href: '/train/monitoring',
			match: seg('/train/monitoring'),
			icon: Activity,
		},
		{ title: 'Analysis', href: '/train/analysis', match: seg('/train/analysis'), icon: ChartLine },
		{ title: 'Models', href: '/train/models', match: seg('/train/models'), icon: Boxes },
	],
};
