/**
 * Turning a MAP SELECTION into a labelling batch.
 *
 * The atlas could always lasso a region — point-in-polygon in data space, plus box, cluster and
 * legend picks — and the selection dead-ended in a results table. There was no way to say "these
 * four hundred points are `letter`", so the cheapest high-volume labelling gesture in the product
 * produced nothing. `SendToProjectDialog` even declares `origin: 'atlas'`; nothing ever passed it.
 *
 * Why the atlas is the right surface for this: a region of an embedding projection is usually a
 * SEMANTIC cluster, so one lasso is very often one label.
 *
 * Pure and separate from any component, because the rules below are the part worth pinning — and the
 * dangerous one is the cap, where the honest answer and the convenient one differ.
 */

/** The server's own bound, mirrored: `SendItemsRequest.items` declares `max_length=1000`, and `send`
 *  refuses on `len(items) * consensus_n > 1000`.
 *
 *  The explorer's send path had NO cap at all, so a big lasso posted straight into a 422. Mirrored
 *  here rather than shared from the annotator zone: a zone does not import another zone, and a
 *  package for one integer would be worse than two comments pointing at the server. */
export const SEND_TASK_CAP = 1000;

/** One pre-annotation a bulk label attaches — the sender's half of the service's `PredictionShape`. */
export interface PredictionShape {
	shape_type: string;
	label: string;
}

/**
 * The prediction for a bulk label, or nothing when no label was chosen.
 *
 * ONE whole-item `tag`: bulk classification has no geometry to draw, and a fabricated full-frame box
 * would be a claim about WHERE the thing is. No `source` — the server stamps that, and a sender able
 * to write it could mark items nobody looked at as human work.
 */
export function predictionFor(label: string): PredictionShape[] | undefined {
	const name = label.trim();
	return name ? [{ shape_type: 'tag', label: name }] : undefined;
}

/**
 * Why this selection cannot be sent, or `null` when it can.
 *
 * The load-bearing case is the third one. The atlas fetches selection detail capped
 * (`.slice(0, 1000)`) while reporting the TRUE selection size, so a lasso over five thousand points
 * yields one thousand hits and a total of five thousand. Sending the hits would queue a fifth of
 * what the user selected and report success — the worst kind of wrong, because the number on screen
 * said five thousand. Refuse and name it instead.
 *
 * `fetched < total` is checked SEPARATELY from the cap rather than folded into it. Today they
 * coincide (the fetch slice and the send cap are both 1000), and if someone changes one the two stop
 * agreeing — at which point this still refuses instead of silently truncating.
 */
export function sendRefusal(opts: {
	fetched: number;
	total: number;
	cap?: number;
	projectChosen?: boolean;
}): string | null {
	const { fetched, total, cap = SEND_TASK_CAP, projectChosen = true } = opts;
	if (total === 0) return 'Lasso a region on the map, or click a cluster, to select items.';
	if (total > cap) {
		return `${total.toLocaleString()} items selected exceeds the ${cap.toLocaleString()} limit for one send — narrow the selection.`;
	}
	if (fetched < total) {
		return `Only ${fetched.toLocaleString()} of ${total.toLocaleString()} selected items have loaded — narrow the selection so all of them can be sent.`;
	}
	if (!projectChosen) return 'Choose a labeling task to send into.';
	// A label is deliberately OPTIONAL and so is never a refusal: sending unlabelled queues the region
	// for someone to draw on, which is the ordinary send and has to keep working.
	return null;
}

/**
 * The key paths for a selection.
 *
 * `keyPath` is passed in rather than imported so this stays testable without a descriptor — and so
 * it is unmistakably the CALLER's descriptor doing the work. Every key in the estate is built the
 * same way (`view.keyPath(row).join('/')`, percent-encoded per part); a key assembled differently
 * looks right in a list and resolves against nothing at the write boundary.
 */
export function selectionKeys<T>(hits: readonly T[], keyPath: (hit: T) => string[]): string[] {
	const keys = hits.map((hit) => keyPath(hit).join('/')).filter(Boolean);
	// De-duplicated: two tasks for one item is two people labelling it.
	return [...new Set(keys)];
}
