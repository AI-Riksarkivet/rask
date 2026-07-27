import { FlaskConical, Package, Workflow } from '@lucide/svelte';
import { exact, seg, type ZoneNav } from '@rask/ui/shell';

// The models area's sidebar routes inside the lakehouse zone. Registry sits at the zone root, so it matches `exact`
// (a `seg` there would light it up on every sibling sub-route); the sub-pages use `seg`.
export const MODELS_ZONE_NAV: ZoneNav = {
	title: 'Models',
	leaves: [
		{
			title: 'Registry',
			href: '/lakehouse/models',
			match: exact('/lakehouse/models'),
			icon: Package,
		},
		{
			title: 'Pipeline',
			href: '/lakehouse/models/pipeline',
			match: seg('/lakehouse/models/pipeline'),
			icon: Workflow,
		},
		{
			title: 'Experiments',
			href: '/lakehouse/models/experiments',
			match: seg('/lakehouse/models/experiments'),
			icon: FlaskConical,
		},
	],
};
