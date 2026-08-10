/**
 * What to do when the ANNOTATIONS change underneath you.
 *
 * #73: the annotator's Arrow table was read exactly once, at mount. Two people labelling the same
 * page never saw each other's work; neither did one person with the item open in two tabs. The
 * second save then went out against a `base_version` from before the first, which is precisely the
 * conflict OCC exists to refuse — so the second annotator's work was rejected AFTER they did it.
 *
 * The cursor tells us the table moved. This module decides what that MEANS, and the decision is
 * where the danger is: reloading is destructive when there is unsaved work on screen.
 */

/** What the canvas should do about a change it did not make. */
export type RemoteChange =
	| { action: 'ignore' }
	| { action: 'reload' }
	| { action: 'warn'; reason: string };

export interface CanvasState {
	/** Has this canvas got uncommitted edits? */
	dirty: boolean;
	/** Is a save in flight right now? */
	saving: boolean;
	/** Is the controller bound to an engine at all? */
	attached: boolean;
}

/**
 * NEVER reload over unsaved work. That is the whole ruling.
 *
 * A silent reload would discard shapes an annotator drew and cannot get back — the single worst
 * outcome available to an annotation tool, and worse than the staleness it fixes, because staleness
 * is recoverable and lost work is not. So a dirty canvas gets TOLD, and the person chooses.
 *
 * Reloading a clean canvas is safe and invisible: nothing on screen is theirs yet, so replacing it
 * with the newer truth loses nothing and quietly fixes the two-tab and two-annotator cases.
 */
export function onRemoteChange(state: CanvasState): RemoteChange {
	// Nothing is bound, so there is no table to refresh and no edits to protect. `ImageViewer` leaves
	// the controller unattached whenever the annotations read failed (#72) — reloading there would
	// fight a load that is already broken.
	if (!state.attached) return { action: 'ignore' };

	// A save in flight is about to change the version itself. Reloading mid-flight would race our
	// own write and could show the pre-save state as if it were newer.
	if (state.saving) return { action: 'ignore' };

	if (state.dirty) {
		return {
			action: 'warn',
			// Names the fact and the consequence, because "changed" alone leaves someone unable to
			// decide. They need to know their save will be REFUSED, not merely that something moved.
			reason:
				'Someone else saved this item. Your unsaved edits are still here, but saving will be refused until you reload.',
		};
	}

	return { action: 'reload' };
}
