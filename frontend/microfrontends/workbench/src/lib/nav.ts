import { LayoutDashboard } from '@lucide/svelte';
import { exact, type ZoneNav } from '@rask/ui/shell';

/**
 * ONE leaf, deliberately — this zone renders its OWN sidebar.
 *
 * The workbench has a single surface: the dock. Its real navigation is the SAVED VIEWS list
 * (`ViewSidebar.svelte`), which is page chrome — it changes as the user works — and cannot live in a
 * `ZoneNav`, which is a static route table. A second rail beside it would be a one-row duplicate of a
 * page you are already on.
 *
 * `zone-shell.test.ts` requires >1 leaf for every other zone because the shell hides a rail below two,
 * and a zone that SILENTLY renders no sidebar is a defect. That reasoning does not apply here: this
 * zone renders a sidebar, just not the shell's. The exemption is named in that test rather than
 * granted by accident.
 */
export const WORKBENCH_ZONE_NAV: ZoneNav = {
	title: 'Workbench',
	groups: [
		{
			label: 'Workbench',
			items: [
				{ title: 'Dock', href: '/workbench/', match: exact('/workbench'), icon: LayoutDashboard },
			],
		},
	],
};
