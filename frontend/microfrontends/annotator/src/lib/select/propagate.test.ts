/**
 * The propagation THRESHOLD (#87, `open_browse.md` §5).
 *
 * Labelling a few items and pushing the label to their neighbours is the highest-leverage action on
 * the browse surface and the easiest way to mislabel a corpus at scale. `n` alone says "give me
 * forty" whether or not forty are actually alike — the fortieth then gets the label because it was
 * RETURNED, not because it resembled anything.
 *
 * The plan's requirement is that both knobs be VISIBLE and adjustable. These pin the cutoff's
 * behaviour and, as much as a unit test can, its honesty: the sentence beside the slider has to name
 * what was excluded, because a threshold that silently eats candidates is the failure mode.
 */

import { describe, expect, it } from 'vitest';

import {
	DEFAULT_MAX_DISTANCE,
	describeCutoff,
	distanceBounds,
	withinDistance,
	type SimilarHit,
} from './similar';

const hit = (d?: number): SimilarHit => (d === undefined ? {} : { _distance: d });

describe('the cutoff', () => {
	it('keeps neighbours at or inside the distance', () => {
		const kept = withinDistance([hit(0.1), hit(0.5), hit(0.9)], 0.5);

		expect(kept).toHaveLength(2);
	});

	it('is INCLUSIVE at the boundary', () => {
		// A candidate exactly at the cutoff is inside it. Exclusive would make the slider's own
		// displayed value the first thing it drops, which reads as an off-by-one to anyone watching
		// the count while dragging.
		expect(withinDistance([hit(0.5)], 0.5)).toHaveLength(1);
	});

	it('drops everything when the cutoff is below the nearest', () => {
		expect(withinDistance([hit(0.4), hit(0.8)], 0.1)).toEqual([]);
	});

	it('KEEPS a hit with no distance at all', () => {
		// The service omits `_distance` when a corpus declares no vector space. Dropping those would
		// report "no matches" for what is really "this corpus cannot answer that question" — two
		// different facts, and only one of them is the user's doing.
		expect(withinDistance([hit(), hit()], 0.01)).toHaveLength(2);
	});

	it('defaults generously rather than hiding candidates on first open', () => {
		// A cutoff that hides results before anyone has touched it teaches people the feature is
		// broken. The bounds readout is what invites tightening.
		expect(withinDistance([hit(0.9)], DEFAULT_MAX_DISTANCE)).toHaveLength(1);
	});
});

describe('the bounds the slider is drawn against', () => {
	it('reports the nearest and furthest actually returned', () => {
		expect(distanceBounds([hit(0.4), hit(0.1), hit(0.9)])).toEqual({
			nearest: 0.1,
			furthest: 0.9,
		});
	});

	it('is null when nothing carries a distance, so no slider is drawn', () => {
		// Better than a 0–1 default range: a control bounded to a range where nothing lives is a
		// control that does nothing, which is worse than its absence.
		expect(distanceBounds([hit(), hit()])).toBeNull();
	});

	it('is null for an empty result', () => {
		expect(distanceBounds([])).toBeNull();
	});
});

describe('what the cutoff SAYS it is doing', () => {
	it('names the number EXCLUDED, not only the number kept', () => {
		// "18 of 24" leaves the six invisible. The dropped count is what someone needs in order to
		// decide the cutoff is too strict — the whole difference between a visible threshold and a
		// silent one.
		expect(describeCutoff(24, 18)).toContain('6 beyond the cutoff');
	});

	it('says so plainly when nothing is excluded', () => {
		expect(describeCutoff(24, 24)).toBe('all 24 within the cutoff');
	});

	it('does not claim a cutoff excluded anything when there were no candidates', () => {
		expect(describeCutoff(0, 0)).toBe('no candidates');
	});
});
