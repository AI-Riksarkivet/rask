/**
 * Labeling WITH embeddings in the grid — the pure half (open-bulk, spec §5 "Embeddings").
 *
 * The reason bulk sees all items at once is set-level operations, and the highest-leverage one
 * is "this row — the others like it": anchor a row, the grid re-orders by embedding distance
 * and a cutoff narrows it to the true neighborhood, and every set-level action (▶ column
 * apply, ✓ accepts, filters) then operates on that neighborhood. The neighbours come from the
 * SAME `/api/search/similar` wire the select surface uses — one embedding seam, two surfaces.
 *
 * Pure and separately pinned for the same reason `corpus-rows.ts` is: the dangerous part is
 * matching a returned hit back to the grid row it names. Mis-association would show one item's
 * distance on another item's row — silently, and looking exactly like data.
 *
 * KEY CONVENTION. A task's `source.keys` elements are UNIT KEY-PATHS (`'doc1/0/1'`) — the
 * zone-wide shape every spec and `taskCanvasHref` already use — so a row's key is the elements
 * joined by `/`, and a hit's key is `keyOfHit` (identity fields joined by `/`, URI-encoded per
 * part — the select surface's convention, identity for ordinary alphanumeric ids). A doc id
 * that itself contains `/` is ambiguous under this join; that ambiguity is the estate's,
 * inherited here, not introduced here.
 */

import { keyOfHit, type SimilarHit } from '$lib/select/similar';

/** The minimal task shape this module reads — structural, so tests need no full TaskDetail. */
export interface KeyedTask {
	source: { keys: string[] };
}

/** The row's key: unit key-paths joined — also the `/api/search/similar` seed for ≈. */
export function taskKeyOf(task: KeyedTask): string {
	return (task.source.keys ?? []).join('/');
}

export interface Neighborhood {
	/** row key → cosine distance. A key PRESENT with `undefined` distance is a real neighbour
	 *  from a corpus that declares no vector space (the service omits `_distance` there) —
	 *  different from an ABSENT key, which was simply not among the n nearest. The two must not
	 *  collapse: one is "cannot rank", the other "ranked out". */
	distances: Map<string, number | undefined>;
	/** False when NO hit carried a distance — the corpus cannot answer, so filtering by
	 *  distance would present "nothing is similar" for "this question has no answer here". */
	hasDistances: boolean;
}

/** Read the hits into an association map, keyed the way every grid row is. */
export function neighborhoodOf(
	hits: readonly SimilarHit[],
	keyFields: readonly string[],
): Neighborhood {
	const distances = new Map<string, number | undefined>();
	let hasDistances = false;
	if (keyFields.length > 0) {
		for (const hit of hits) {
			if (keyFields.some((f) => hit[f] === undefined || hit[f] === null)) continue; // never guess
			const d = typeof hit._distance === 'number' ? hit._distance : undefined;
			if (d !== undefined) hasDistances = true;
			const key = keyOfHit(hit, keyFields);
			if (!distances.has(key)) distances.set(key, d); // first (nearest) mention wins
		}
	}
	return { distances, hasDistances };
}

/** The row's distance to the anchor, or undefined (not a neighbour / unrankable corpus). */
export function distanceOf(task: KeyedTask, hood: Neighborhood): number | undefined {
	return hood.distances.get(taskKeyOf(task));
}

/**
 * Is this row inside the anchored neighborhood at the cutoff?
 *
 * Three honest regimes: an unrankable corpus keeps EVERY row (hiding would lie); a row not
 * among the n nearest is out (it is farther than everything ranked); a ranked row obeys the
 * cutoff. Mirrors the select surface's `withinDistance` stance on missing distances.
 */
export function inNeighborhood(task: KeyedTask, hood: Neighborhood, cutoff: number): boolean {
	if (!hood.hasDistances) return true;
	const key = taskKeyOf(task);
	if (!hood.distances.has(key)) return false;
	const d = hood.distances.get(key);
	return d === undefined || d <= cutoff;
}

/** Rows re-ordered nearest-first; unranked rows keep their relative order at the end. */
export function orderedByDistance<T extends KeyedTask>(
	tasks: readonly T[],
	hood: Neighborhood,
): T[] {
	return [...tasks].sort((a, b) => {
		const da = distanceOf(a, hood);
		const db = distanceOf(b, hood);
		if (da === undefined && db === undefined) return 0;
		if (da === undefined) return 1;
		if (db === undefined) return -1;
		return da - db;
	});
}
