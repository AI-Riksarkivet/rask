/**
 * Turning a browse selection into items a project can receive.
 *
 * Step 1 of the bulk-labeling design (`open_browse.md`): the browse page could always FIND keys, and
 * could only ever open them one at a time on the canvas. Sending them into a project was a
 * per-project dialog you had to already be standing in, which meant the natural workflow — "find
 * five hundred pages that look like this, label them" — had no path through the product at all.
 *
 * Pure and separate from any component, because the shaping rules below are the part worth pinning:
 * they decide what a project actually receives, and getting them wrong is invisible until someone
 * opens a task and finds the wrong thing in it.
 */

/** One item as the send command's schema expects it. */
export interface SendableItem {
	source: { kind: string; keys: string[]; where?: string | null };
	media: { kind: string };
}

/** How many items one bulk send may carry.
 *
 *  A cap, not a page size: every item becomes its own task actor, so an unbounded send is an
 *  unbounded number of actors from one click. 5 000 is an honest ceiling rather than a tuned one —
 *  it is large enough for the workflows this exists for and small enough that the cost is bounded.
 *  Named here so the UI and any future server-side guard can quote the same number. */
export const MAX_BULK_ITEMS = 5000;

/**
 * Shape a selection into sendable items.
 *
 * ONE key per item, deliberately. A task is one thing a person looks at and decides about; batching
 * several keys into one task would make the queue's counts, the review verdict and the per-annotator
 * metrics all describe something other than what a reviewer actually saw.
 *
 * The dataset rides on every item as `where`. Without it a project's items resolve against the
 * DEFAULT corpus, which is the stale-item defect #37 fixed at the write boundary — the send would be
 * refused, correctly, but only after someone had waited for it.
 */
export function itemsFromSelection(
	keys: readonly string[],
	dataset: string | null,
	mediaKind = 'image',
): SendableItem[] {
	const unique = [...new Set(keys.map((k) => k.trim()).filter(Boolean))];
	return unique.map((key) => ({
		source: { kind: 'chunks', keys: [key], ...(dataset ? { where: dataset } : {}) },
		media: { kind: mediaKind },
	}));
}

/** Why a bulk send cannot proceed, or `null` when it can. */
export function refuseReason(keys: readonly string[], projectId: string | null): string | null {
	const count = itemsFromSelection(keys, null).length;
	if (count === 0) return 'Select at least one item to send.';
	if (projectId === null) return 'Choose a labeling task to send into.';
	if (count > MAX_BULK_ITEMS) {
		// Named rather than silently truncated: a send that quietly dropped the tail would leave
		// someone believing a corpus was queued when most of it was not.
		return `${count.toLocaleString()} items exceeds the ${MAX_BULK_ITEMS.toLocaleString()} limit for one send — narrow the selection.`;
	}
	return null;
}
