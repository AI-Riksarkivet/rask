/**
 * Building a labelling batch from one example.
 *
 * These pin the parts that are invisible when wrong. A neighbour key assembled differently from
 * every other key in the zone looks right in the list and fails at the send boundary; a merge that
 * does not dedupe queues one page twice, which is two people labelling it; and a candidate that
 * cannot say why it is here makes the whole surface untrustworthy in exactly the situation it
 * exists for — accepting several hundred items at once.
 */

import { describe, expect, it } from 'vitest';

import { describeNeighbour, keyOfHit, mergeSelection, newlyAdded } from './similar';

const KEYS = ['doc_id', 'speech_id', 'chunk_id'];

describe('a neighbour becomes a selectable key', () => {
	it('joins the identity fields the same way the rest of the zone does', () => {
		const key = keyOfHit({ doc_id: 'd1', speech_id: 2, chunk_id: 3 }, KEYS);

		expect(key).toBe('d1/2/3');
	});

	it('STRINGIFIES numeric fields', () => {
		// Lance returns speech/chunk ids as numbers. A key built by concatenation without this is
		// still a string, so nothing errors — it just never matches the corpus at send.
		expect(keyOfHit({ doc_id: 'd1', speech_id: 0, chunk_id: 0 }, KEYS)).toBe('d1/0/0');
	});

	it('keeps the field ORDER of the identity, not the object', () => {
		// Object key order follows insertion. Reading the identity's order instead is what makes the
		// key stable across a service that reorders its projection.
		const key = keyOfHit({ chunk_id: 3, doc_id: 'd1', speech_id: 2 }, KEYS);

		expect(key).toBe('d1/2/3');
	});

	it('renders a missing field as empty rather than "undefined"', () => {
		// `undefined` stringifies to the literal text "undefined", which would be a key that looks
		// plausible and matches nothing.
		expect(keyOfHit({ doc_id: 'd1' }, KEYS)).toBe('d1//');
	});

	it('PERCENT-ENCODES each part, exactly like DatasetView.keyPath', () => {
		// The one that would never show up in ordinary testing: for an alphanumeric id
		// `encodeURIComponent` is the identity function, so a version without it looks correct
		// everywhere until an id carries a space or a slash. A raw slash is the worst case — it adds a
		// SEGMENT, so the key silently gains an arity the corpus's identity does not have and the
		// service refuses it with a message about part counts nobody can trace back to here.
		expect(keyOfHit({ doc_id: 'a b', speech_id: 'x/y', chunk_id: 3 }, KEYS)).toBe('a%20b/x%2Fy/3');
	});
});

describe('every candidate says why it is here', () => {
	it('names the rank and the distance', () => {
		expect(describeNeighbour(0, 0.1234)).toBe('1st nearest · 0.123');
	});

	it('handles the 11th-13th ordinals every naive version gets wrong', () => {
		// And they land on the first page of results, where someone would actually see it.
		expect(describeNeighbour(10, 0.5)).toMatch(/^11th/);
		expect(describeNeighbour(11, 0.5)).toMatch(/^12th/);
		expect(describeNeighbour(12, 0.5)).toMatch(/^13th/);
		expect(describeNeighbour(20, 0.5)).toMatch(/^21st/);
	});

	it('still names the rank when the service sent no distance', () => {
		expect(describeNeighbour(2, undefined)).toBe('3rd nearest');
	});
});

describe('adding neighbours to a selection', () => {
	it('de-duplicates, because two tasks for one page is two people labelling it', () => {
		// Not hypothetical: neighbours-of-A and neighbours-of-B overlap heavily by construction when
		// A and B are themselves similar, which is exactly when someone runs both.
		expect(mergeSelection(['a', 'b'], ['b', 'c'])).toEqual(['a', 'b', 'c']);
	});

	it('preserves the order already selected', () => {
		expect(mergeSelection(['z', 'a'], ['m'])).toEqual(['z', 'a', 'm']);
	});

	it('drops blank keys rather than selecting nothing', () => {
		expect(mergeSelection(['a'], ['', 'b'])).toEqual(['a', 'b']);
	});

	it('reports how many were actually NEW', () => {
		// "Added 24" when 20 were already selected is a lie the user acts on — they think the batch
		// grew by 24 and size the next step accordingly.
		expect(newlyAdded(['a', 'b'], ['b', 'c', 'd'])).toBe(2);
	});

	it('counts a duplicated incoming key once', () => {
		expect(newlyAdded([], ['a', 'a', 'b'])).toBe(2);
	});
});
