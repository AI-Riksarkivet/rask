/**
 * The COMPARE view's reading + writing model — preference judgments over a multi-key item.
 *
 * A multi-key item (`?keys=a,b,…`) is a scope: the canvas walks it one unit at a time, which is
 * the wrong shape for the question "which of these is better?" — a judgment ABOUT the candidates
 * side by side, not about either alone. Label Studio's `Pairwise` and Argilla's preference
 * questions exist for exactly this; CVAT has no equivalent. Ours composes from what is already
 * here: each pane is one unit, and "preferred" is the ontology's `tag` class applied to the
 * winning unit's OWN annotations — the same row a ClassificationBar toggle writes, so the
 * judgment rides the ordinary save transport, versions and review queue rather than a parallel
 * verdict store.
 *
 * Writes go straight through `loadAnnotations`/`postSave` per pane — NOT through a canvas
 * controller. The controller's autosave is a 4-second debounce with no unmount flush, so driving
 * a preference by navigating the canvas and toggling there would race the debounce on every
 * flip; a direct version-guarded delta per pane cannot lose the judgment. The compare view
 * therefore REPLACES the canvas while it is open (one writer per unit at a time).
 */
import type { Table } from 'apache-arrow';

import { makeInsertRow, type SavePayload } from '@rask/labeling/annotations-client';

import { projectRows } from './annotation-rows';
import type { TextLaneLine } from './text-lane';
import { textLaneLines } from './text-lane';

/** One saved item-level tag on a pane — enough identity to delete it, enough label to show it. */
export interface PaneTag {
	id: string;
	label: string;
}

/** The item-level tags a pane's saved table carries (its active choices). */
export function paneTags(table: Table): PaneTag[] {
	return projectRows(table, new Map(), [])
		.filter((r) => r.shape === 'tag' && r.label !== '')
		.map((r) => ({ id: r.id, label: r.label }));
}

/** The pane's readable body for a TEXT unit — the same lines the transcription lane shows. */
export function paneTextLines(table: Table): TextLaneLine[] {
	return textLaneLines(projectRows(table, new Map(), []));
}

/** The delta ONE chip toggle means for a pane: delete the tag row when the choice is already
 *  applied, insert one when it is not. Version-guarded — `base_version` is the version the pane
 *  was READ at, so a concurrent writer surfaces as a 409 instead of a silent overwrite. */
export function toggleTagPayload(
	tags: readonly PaneTag[],
	label: string,
	version: number,
): SavePayload {
	const existing = tags.find((t) => t.label === label);
	return {
		edits: [],
		inserts: existing
			? []
			: [
					makeInsertRow({
						shape_type: 'tag',
						label,
						t_start: 0,
						t_end: 0,
						status: 'accepted',
						source: 'human',
					}),
				],
		geometry: [],
		temporal: [],
		spans: [],
		deletes: existing ? [existing.id] : [],
		base_version: version,
	};
}

/** Pane ordinals read as candidates, not pages: A, B, C… (falls back to numbers past Z). */
export function paneOrdinal(i: number): string {
	return i < 26 ? String.fromCharCode(65 + i) : String(i + 1);
}
