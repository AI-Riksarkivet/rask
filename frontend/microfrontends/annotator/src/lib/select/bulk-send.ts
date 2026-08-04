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

/** One pre-annotation a bulk action attaches — the sender's half of `PredictionShape`. */
export interface PredictionShape {
	shape_type: string;
	label: string;
}

/** One item as the send command's schema expects it. */
export interface SendableItem {
	source: { kind: string; keys: string[]; where?: string | null; dataset_version?: number | null };
	media: { kind: string };
	/** Pre-annotations for this item. Absent for an ordinary send — see `itemsFromSelection`. */
	prediction?: PredictionShape[];
}

/** The server's cap, mirrored — `SendItemsRequest.items` declares `max_length=1000` and `send`
 *  refuses on `len(items) * consensus_n > 1000`.
 *
 *  This used to be 5 000, a number of this file's own invention, and the disagreement was invisible:
 *  a 2 000-item selection passed every check here and came back 422 from a server that had already
 *  been asked to do the impossible. A client-side cap that disagrees with the server is not a cap,
 *  it is a delay before a refusal. Named once so both halves quote the same number. */
export const SEND_TASK_CAP = 1000;

/** How many ITEMS may be sent into a project with this replica count.
 *
 *  The cap is on TASKS, and consensus makes N of them per item — so 400 items into a 3-replica
 *  project is 1 200 tasks and is refused, however reasonable 400 looks. A nonsensical count is
 *  treated as one rather than dividing by zero: refusing everything would be a worse answer than
 *  assuming the default. */
export function maxItemsFor(consensusN: number): number {
	return Math.floor(SEND_TASK_CAP / Math.max(1, Math.floor(consensusN) || 1));
}

/** The class names a bulk label may offer, given a project's taxonomy.
 *
 *  A bulk label asserts "this whole item is an X", which is what the `tag` primitive means. A class
 *  drawn only as a box is not something that assertion can be made with — and the server would
 *  refuse it, because `membership_violation` checks the label WITH the primitive it was drawn with.
 *  Offering such a class would be offering a guaranteed 409.
 *
 *  An empty `tools` list is UNCONSTRAINED (the same reading `ontologyTools` takes), so those classes
 *  are offered. */
export function bulkLabelChoices(
	ontology: { classes?: { name: string; tools?: string[] }[] } | undefined,
): string[] {
	return (ontology?.classes ?? [])
		.filter((c) => (c.tools?.length ?? 0) === 0 || c.tools!.includes('tag'))
		.map((c) => c.name);
}

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
	datasetVersion: number | null = null,
	mediaKind = 'image',
	label = '',
): SendableItem[] {
	const unique = [...new Set(keys.map((k) => k.trim()).filter(Boolean))];
	return unique.map((key) => ({
		source: {
			kind: 'chunks',
			keys: [key],
			...(dataset ? { where: dataset } : {}),
			// The VERSION the item was taken from. `publish.py` records it into the publish plan's
			// `dataset_versions`, so an item sent without one makes the published artifact unable to
			// say which version of the corpus it describes. The explorer's sender has always carried
			// it; this one did not, and the annotator's wire schema silently stripped it anyway.
			...(datasetVersion !== null ? { dataset_version: datasetVersion } : {}),
		},
		media: { kind: mediaKind },
		// The bulk LABEL, as one whole-item tag. `tag` because bulk classification has no geometry to
		// draw and a fabricated full-frame box would be a claim about WHERE the thing is.
		//
		// ABSENT when no label was picked, rather than an empty array: absent means "queue these
		// blank, someone will draw them", which is the ordinary send and must stay byte-identical.
		// The server stamps `source` on whatever arrives here — this end cannot and must not.
		...(label ? { prediction: [{ shape_type: 'tag', label }] } : {}),
	}));
}

/** Why a bulk send cannot proceed, or `null` when it can.
 *
 *  `consensusN` is the chosen project's replica count, because the cap it is measured against is on
 *  TASKS rather than items — see `maxItemsFor`. */
export function refuseReason(
	keys: readonly string[],
	projectId: string | null,
	consensusN = 1,
): string | null {
	const count = itemsFromSelection(keys, null, null).length;
	if (count === 0) return 'Select at least one item to send.';
	if (projectId === null) return 'Choose a labeling task to send into.';
	const cap = maxItemsFor(consensusN);
	if (count > cap) {
		// Named rather than silently truncated: a send that quietly dropped the tail would leave
		// someone believing a corpus was queued when most of it was not. The replica count is named
		// too when it is what closed the door — "400 exceeds 333" is baffling without it.
		const why = consensusN > 1 ? ` (${SEND_TASK_CAP.toLocaleString()} tasks ÷ ${consensusN} replicas)` : '';
		return `${count.toLocaleString()} items exceeds the ${cap.toLocaleString()} limit for one send${why} — narrow the selection.`;
	}
	return null;
}
