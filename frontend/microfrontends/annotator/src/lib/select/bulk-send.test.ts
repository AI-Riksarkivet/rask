/**
 * Turning a browse selection into items a project receives.
 *
 * Browse could always FIND keys and could only ever open them one at a time. Sending them into a
 * project was a per-project dialog you had to already be standing in — so "find five hundred pages
 * like this one, label them" had no path through the product.
 *
 * The shaping rules are what these pin: they decide what a project actually receives, and getting
 * them wrong is invisible until someone opens a task and finds the wrong thing in it.
 */

import { describe, expect, it } from 'vitest';

import * as v from 'valibot';

import { SendItemSchema } from '$lib/projects/types';
import { itemsFromSelection, MAX_BULK_ITEMS, refuseReason } from './bulk-send';

describe('shaping a selection', () => {
	it('makes ONE item per key', () => {
		// A task is one thing a person looks at and decides about. Batching keys into one task would
		// make the queue counts, the review verdict and the per-annotator metrics all describe
		// something other than what a reviewer saw.
		const items = itemsFromSelection(['a', 'b', 'c'], null);

		expect(items).toHaveLength(3);
		expect(items.map((i) => i.source.keys)).toEqual([['a'], ['b'], ['c']]);
	});

	it('carries the DATASET on every item', () => {
		// Without it a project's items resolve against the default corpus — the stale-item defect #37
		// fixed at the write boundary. The send would be refused correctly, but only after a wait.
		const items = itemsFromSelection(['a', 'b'], 'vasa');

		expect(items.every((i) => i.source.where === 'vasa')).toBe(true);
	});

	it('OMITS `where` for the default corpus rather than sending null', () => {
		// Absent means "the default"; an explicit null is a different statement.
		expect('where' in itemsFromSelection(['a'], null)[0]!.source).toBe(false);
	});

	it('de-duplicates, because two tasks for one page is two people labelling it', () => {
		expect(itemsFromSelection(['a', 'a', 'b'], null)).toHaveLength(2);
	});

	it('drops blank and whitespace-only keys', () => {
		// A pasted list ends with a newline more often than not.
		expect(itemsFromSelection(['a', '', '   ', 'b'], null)).toHaveLength(2);
	});

	it('trims, so a pasted key does not become a different key', () => {
		expect(itemsFromSelection([' a '], null)[0]!.source.keys).toEqual(['a']);
	});
});

describe('when a send is refused', () => {
	it('refuses an empty selection', () => {
		expect(refuseReason([], 'p1')).toMatch(/at least one/i);
	});

	it('refuses with no project chosen', () => {
		expect(refuseReason(['a'], null)).toMatch(/choose a labeling task/i);
	});

	it('NAMES the cap rather than truncating', () => {
		// A send that quietly dropped the tail would leave someone believing a corpus was queued when
		// most of it was not. Every item becomes its own task actor, so the cap is a real bound.
		const tooMany = Array.from({ length: MAX_BULK_ITEMS + 1 }, (_, i) => `k${i}`);

		const reason = refuseReason(tooMany, 'p1');

		expect(reason).toContain(MAX_BULK_ITEMS.toLocaleString());
		expect(reason).toMatch(/narrow the selection/i);
	});

	it('counts UNIQUE keys against the cap', () => {
		// Otherwise a duplicated paste would refuse a selection that is actually within bounds.
		const withDupes = [...Array.from({ length: MAX_BULK_ITEMS }, (_, i) => `k${i}`), 'k0', 'k1'];

		expect(refuseReason(withDupes, 'p1')).toBeNull();
	});

	it('allows a valid selection', () => {
		expect(refuseReason(['a', 'b'], 'p1')).toBeNull();
	});
});


describe('dataset provenance', () => {
	it('carries the dataset VERSION on every item', () => {
		// `publish.py` records it into the publish plan's `dataset_versions`. An item sent without one
		// leaves the published artifact unable to say which version of the corpus it describes.
		const items = itemsFromSelection(['a', 'b'], 'vasa', 7);

		expect(items.every((i) => i.source.dataset_version === 7)).toBe(true);
	});

	it('omits the version when there is none, rather than sending null', () => {
		expect('dataset_version' in itemsFromSelection(['a'], 'vasa', null)[0]!.source).toBe(false);
	});

	it('SURVIVES the wire schema — the field was being stripped', () => {
		// The defect this pins. valibot's `v.object` drops unknown keys, and the annotator's send
		// schema did not declare `dataset_version` — so every item sent from this zone recorded no
		// version, while the explorer (whose schema declares it) recorded the real one. Two senders,
		// one contract, and only one honoured it. Nothing errored; the field simply vanished.
		const [item] = itemsFromSelection(['a'], 'vasa', 7);

		const parsed = v.parse(SendItemSchema, item);

		expect(parsed.source.dataset_version).toBe(7);
	});
});
