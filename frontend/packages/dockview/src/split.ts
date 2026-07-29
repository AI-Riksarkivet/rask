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

/** One direction, as every surface that offers a split needs to describe it. */
export interface SplitDirection {
	/** The drop-anchor value {@link splitPanel} takes. */
	readonly position: SplitPosition;
	/** The word the header title and the context-menu row both use — one wording, one place. */
	readonly word: 'up' | 'down' | 'left' | 'right';
	/** Which cell of the 3×3 compass pad this occupies. */
	readonly cell: 'n' | 's' | 'w' | 'e';
	/** Degrees to rotate the ONE arrow glyph, rather than importing four icons. */
	readonly rotate: 0 | 90 | 180 | -90;
}

/**
 * The four directions, in reading order for a compass pad — and the context menu's row order.
 *
 * Exists because the list was written out TWICE with different orders: the header offered right and
 * down only (`GroupActions.svelte`), while `context-menu.ts` hardcoded its own tuple array as
 * right/left/down/up. Two hardcoded lists of the same four things is how a surface silently ends up
 * offering three, and neither copy could be tested against the other.
 */
export const SPLIT_DIRECTIONS: readonly SplitDirection[] = [
	{ position: 'top', word: 'up', cell: 'n', rotate: 0 },
	{ position: 'left', word: 'left', cell: 'w', rotate: -90 },
	{ position: 'right', word: 'right', cell: 'e', rotate: 90 },
	{ position: 'bottom', word: 'down', cell: 's', rotate: 180 },
] as const;

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

/**
 * The verb a control should show, so the button never promises something it will not do.
 *
 * Takes the COUNT, not the group, and that is load-bearing rather than a tidy-up. The header cluster
 * has to re-derive this as tabs are dragged in and out, but `group.panels` is a plain array behind a
 * getter — reading `.length` inside a `$derived` tracks nothing, so a group-shaped signature forces
 * the caller to keep its own reactive copy of the rule. That is exactly what happened:
 * `GroupActions.svelte` inlined `panelCount > 1 ? 'Split' : 'Duplicate'` and never imported this
 * function, leaving two implementations of one rule that could only disagree in a browser — no unit
 * test could see the drift, because the tested function was the one nothing called.
 */
export const splitVerb = (panelCount: number): 'Split' | 'Duplicate' =>
	panelCount > 1 ? 'Split' : 'Duplicate';
