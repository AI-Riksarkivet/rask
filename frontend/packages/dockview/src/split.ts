/**
 * "Split" — one implementation, used by both the header button and the context-menu row.
 *
 * It exists as its own module because the behaviour has a subtlety that must not be reimplemented
 * twice: `moveTo` onto a panel's OWN group is a **silent no-op** when that group holds a single
 * panel. dockview takes the `sourceGroup.size < 2` branch, treats the operation as moving the whole
 * GROUP, and calls `gridview.moveView(parent, from, to)` with `from === to`. Nothing happens and
 * nothing throws — the button appears broken.
 *
 * So split has two behaviours:
 *
 *  - **2 or more panels** — MOVE the active panel out into a new adjacent group. The real split.
 *  - **exactly 1 panel** — DUPLICATE it into a new adjacent group.
 *
 * Duplicating rather than disabling is deliberate. Every zone seeds three groups of one panel each,
 * so a "disable when nothing to split" guard greys out every split control on first load — which is
 * precisely the "there are no features here" complaint the chrome exists to answer. It is also the
 * VS Code semantic for *Split editor right*, which is what the icon reads as.
 */
import type { DockviewApi, DockviewGroupPanel, IDockviewPanel } from 'dockview';

/** Where the new pane goes, in the drop-anchor vocabulary the group apis use. */
export type SplitPosition = 'left' | 'right' | 'top' | 'bottom';

/**
 * `position` is a drop anchor; `direction` is a grid placement. Two vocabularies for the same four
 * sides, and `top`/`bottom` are spelled `above`/`below` on the grid — mapped explicitly rather than
 * assumed, because the wrong one silently places the pane on the wrong side.
 */
const DIRECTION = {
	left: 'left',
	right: 'right',
	top: 'above',
	bottom: 'below',
} as const;

export function splitPanel(
	containerApi: DockviewApi,
	group: DockviewGroupPanel,
	panel: IDockviewPanel,
	position: SplitPosition,
): void {
	if (group.panels.length > 1) {
		panel.api.moveTo({ group, position });
		return;
	}

	// Ids are unique across the dock, so a duplicate of `runs` cannot also be `runs`. Probe upward
	// rather than appending a random suffix: a persisted layout stays readable to a human, and the
	// same panel duplicated twice reads as `runs-2`, `runs-3`.
	let n = 2;
	while (containerApi.getPanel(`${panel.id}-${n}`) !== undefined) n += 1;

	containerApi.addPanel({
		id: `${panel.id}-${n}`,
		component: panel.api.component,
		title: panel.title ?? panel.id,
		params: panel.params,
		position: { referenceGroup: group, direction: DIRECTION[position] },
	});
}

/** The verb a control should show, so the button never promises something it will not do. */
export const splitVerb = (group: DockviewGroupPanel): 'Split' | 'Duplicate' =>
	group.panels.length > 1 ? 'Split' : 'Duplicate';
