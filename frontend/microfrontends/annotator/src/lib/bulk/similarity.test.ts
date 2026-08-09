/** Anchored similarity in the grid — the hit→row association, pinned pure. */

import { describe, expect, it } from 'vitest';
import {
	distanceOf,
	inNeighborhood,
	neighborhoodOf,
	orderedByDistance,
	taskKeyOf,
} from './similarity.js';

const KEY_FIELDS = ['doc_id', 'speech_id', 'chunk_id'];
// The zone-wide task shape: one element per UNIT key-path.
const task = (key: string) => ({ source: { keys: [key] } });

describe('neighborhoodOf', () => {
	it('associates hits to rows via keyOfHit — identity fields joined, matching the unit key-path', () => {
		const hood = neighborhoodOf(
			[
				{ doc_id: 'doc1', speech_id: 0, chunk_id: 1, _distance: 0.1 },
				{ doc_id: 'doc1', speech_id: 0, chunk_id: 2, _distance: 0.4 },
				{ doc_id: 'doc9', speech_id: 3, chunk_id: 7 }, // unrankable corpus row: no distance
			],
			KEY_FIELDS,
		);
		expect(hood.hasDistances).toBe(true);
		expect(distanceOf(task('doc1/0/1'), hood)).toBe(0.1);
		expect(distanceOf(task('doc1/0/2'), hood)).toBe(0.4);
		expect(distanceOf(task('nope/0/0'), hood)).toBeUndefined();
	});

	it('a hit missing an identity field is skipped, never guessed at', () => {
		const hood = neighborhoodOf([{ doc_id: 'doc1', _distance: 0.1 }], KEY_FIELDS);
		expect(hood.distances.size).toBe(0);
	});

	it('no key fields ⇒ no association (the corpus read has not landed)', () => {
		const hood = neighborhoodOf([{ doc_id: 'doc1', speech_id: 0, chunk_id: 1, _distance: 0 }], []);
		expect(hood.distances.size).toBe(0);
	});
});

describe('inNeighborhood', () => {
	const hood = neighborhoodOf(
		[
			{ doc_id: 'a', speech_id: 0, chunk_id: 0, _distance: 0.1 },
			{ doc_id: 'b', speech_id: 0, chunk_id: 0, _distance: 0.9 },
		],
		KEY_FIELDS,
	);

	it('the cutoff narrows ranked rows; rows outside the n nearest are out entirely', () => {
		expect(inNeighborhood(task('a/0/0'), hood, 0.5)).toBe(true);
		expect(inNeighborhood(task('b/0/0'), hood, 0.5)).toBe(false);
		expect(inNeighborhood(task('b/0/0'), hood, 1)).toBe(true);
		expect(inNeighborhood(task('zzz/0/0'), hood, 1)).toBe(false);
	});

	it('an unrankable corpus keeps EVERY row — hiding would present "nothing similar" for "cannot rank"', () => {
		const flat = neighborhoodOf([{ doc_id: 'a', speech_id: 0, chunk_id: 0 }], KEY_FIELDS);
		expect(flat.hasDistances).toBe(false);
		expect(inNeighborhood(task('anything/0/0'), flat, 0.01)).toBe(true);
	});
});

describe('orderedByDistance', () => {
	it('nearest first; unranked rows keep their relative order at the end', () => {
		const hood = neighborhoodOf(
			[
				{ doc_id: 'far', speech_id: 0, chunk_id: 0, _distance: 0.8 },
				{ doc_id: 'near', speech_id: 0, chunk_id: 0, _distance: 0.05 },
			],
			KEY_FIELDS,
		);
		const rows = [task('x/0/0'), task('far/0/0'), task('y/0/0'), task('near/0/0')];
		expect(orderedByDistance(rows, hood).map(taskKeyOf)).toEqual([
			'near/0/0',
			'far/0/0',
			'x/0/0',
			'y/0/0',
		]);
	});
});

describe('taskKeyOf', () => {
	it('the unit key-path elements joined — one element is the common case, compare items have two', () => {
		expect(taskKeyOf(task('doc1/0/1'))).toBe('doc1/0/1');
		expect(taskKeyOf({ source: { keys: ['a/0/0', 'b/0/0'] } })).toBe('a/0/0/b/0/0');
	});
});
