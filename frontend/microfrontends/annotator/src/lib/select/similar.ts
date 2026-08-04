/**
 * Building a labelling batch from ONE example — the client half of `/api/search/similar`.
 *
 * `open_browse.md` §3: the bulk bar can label a selection, but a selection had to be assembled by
 * hand, one document at a time. The way anyone actually builds a labelling batch is "this page —
 * find me four hundred more like it", and the embeddings to answer that were already in the table.
 *
 * Pure and separate from any component, because the parts worth pinning are the ones that are
 * invisible when wrong: a neighbour whose key is assembled differently from every other key in the
 * zone silently fails to resolve at send, and a merge that does not dedupe queues the same page
 * twice — which is two people labelling it.
 */

/** One neighbour, as the search service returns it: the identity fields plus its distance. */
export type SimilarHit = Record<string, unknown> & { _distance?: number };

/**
 * A hit's key path — identity fields joined by `/`.
 *
 * Byte-identical to `DatasetView.keyPath(row).join('/')`, which is what every other key in this zone
 * is, **including the `encodeURIComponent` per part**. That encoding is the whole reason this
 * function is pinned rather than inlined: for an ordinary alphanumeric id it is the identity
 * function, so a version without it passes every plausible test and diverges only on the ids that
 * contain a space or a slash — where the key looks right in the list and then resolves against
 * nothing at the send boundary.
 */
export function keyOfHit(hit: SimilarHit, keyFields: readonly string[]): string {
	return keyFields.map((f) => encodeURIComponent(String(hit[f] ?? ''))).join('/');
}

/**
 * Why this candidate is here, in words.
 *
 * The design's load-bearing requirement for the preview pane: *every candidate must say why it is
 * here*. A bulk surface that cannot explain its selection is one nobody trusts enough to accept in
 * bulk, which defeats the point of it existing.
 *
 * Distance is shown alongside the rank rather than instead of it — rank is what a person reasons
 * about ("the first ten are obviously right, it gets vague after thirty"), and the raw cosine
 * distance is what lets them find where that happens.
 */
export function describeNeighbour(index: number, distance: number | undefined): string {
	const rank = ordinal(index + 1);
	return distance === undefined ? `${rank} nearest` : `${rank} nearest · ${distance.toFixed(3)}`;
}

function ordinal(n: number): string {
	// 11th/12th/13th are the exception every naive implementation gets wrong, and they fall inside
	// the first page of results where someone would actually see it.
	const teen = n % 100;
	if (teen >= 11 && teen <= 13) return `${n}th`;
	const suffix = { 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] ?? 'th';
	return `${n}${suffix}`;
}

/**
 * Add neighbours to a selection.
 *
 * De-duplicated and order-preserving. Two tasks for one page is two people labelling it, and the
 * overlap here is not hypothetical: finding neighbours of A and then of B, where A and B are
 * themselves similar, returns two heavily overlapping sets by construction.
 */
export function mergeSelection(existing: readonly string[], incoming: readonly string[]): string[] {
	return [...new Set([...existing, ...incoming.filter(Boolean)])];
}

/** How many of `incoming` are not already selected — what an "added N" report should say. */
export function newlyAdded(existing: readonly string[], incoming: readonly string[]): number {
	const have = new Set(existing);
	return new Set(incoming.filter((k) => k && !have.has(k))).size;
}
