import type { DockviewApi } from 'dockview';

/**
 * Allocate a panel id that is free across the whole dock.
 *
 * Not hygiene — `addPanel` THROWS on a duplicate id (`dockview: panel with id X already exists`), so
 * this is the difference between a working button and an uncaught exception inside a click handler.
 * And if a collision ever did get through, `SerializedDockview.panels` is keyed BY ID, so two panels
 * sharing one would silently collapse to a single entry the next time the layout was saved.
 *
 * Note that id and `component` are different strings with different contracts, and conflating them is
 * easy: the COMPONENT key is what a serialized layout resolves a renderer by, so it must stay stable
 * across releases; the ID only has to be unique within one live dock.
 *
 * Probes upward rather than appending randomness: a persisted layout stays readable to a human, and
 * the same panel added three times reads as `runs`, `runs-2`, `runs-3` instead of three uuids.
 *
 * Takes `Pick<DockviewApi, 'getPanel'>` rather than the whole api on purpose — it is the entire
 * dependency, and it lets a test pass a two-line stub instead of standing up a real dock in a repo
 * that has no DOM to stand one up in.
 */
export function uniquePanelId(api: Pick<DockviewApi, 'getPanel'>, base: string): string {
	if (api.getPanel(base) === undefined) return base;
	let n = 2;
	while (api.getPanel(`${base}-${n}`) !== undefined) n += 1;
	return `${base}-${n}`;
}
