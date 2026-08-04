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
import { bulkLabelChoices, itemsFromSelection, maxItemsFor, refuseReason, SEND_TASK_CAP } from './bulk-send';

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
		const tooMany = Array.from({ length: SEND_TASK_CAP + 1 }, (_, i) => `k${i}`);

		const reason = refuseReason(tooMany, 'p1');

		expect(reason).toContain(SEND_TASK_CAP.toLocaleString());
		expect(reason).toMatch(/narrow the selection/i);
	});

	it('counts UNIQUE keys against the cap', () => {
		// Otherwise a duplicated paste would refuse a selection that is actually within bounds.
		const withDupes = [...Array.from({ length: SEND_TASK_CAP }, (_, i) => `k${i}`), 'k0', 'k1'];

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

describe('the cap agrees with the server', () => {
	it('is the SERVER task cap, not a larger number of its own', () => {
		// The defect: this file's cap was 5 000 while `SendItemsRequest.items` declares
		// `max_length=1000`. A 2 000-item selection passed every check here and came back 422 from a
		// server that had already been asked to do the impossible. A client-side cap that disagrees
		// with the server is not a cap, it is a delay before a refusal.
		expect(SEND_TASK_CAP).toBe(1000);
	});

	it('DIVIDES the cap by the replica count', () => {
		// `send` refuses on `len(items) * consensus_n > 1000` — the cap is on TASKS, and consensus
		// makes N tasks per item. 400 items into a 3-replica project is 1 200 tasks: refused server
		// side, silently accepted here until this existed.
		expect(maxItemsFor(1)).toBe(1000);
		expect(maxItemsFor(3)).toBe(333);
	});

	it('refuses a selection that only overflows BECAUSE of consensus', () => {
		const items = Array.from({ length: 400 }, (_, i) => `k${i}`);

		expect(refuseReason(items, 'p1', 1)).toBeNull();
		expect(refuseReason(items, 'p1', 3)).toMatch(/narrow the selection/i);
	});

	it('treats a missing or nonsensical replica count as one', () => {
		expect(maxItemsFor(0)).toBe(1000);
		expect(maxItemsFor(-2)).toBe(1000);
	});
});

describe('the label a bulk action applies', () => {
	it('attaches ONE whole-item tag carrying the class', () => {
		// `tag` is the primitive for "this whole item is an X" — bulk classification has no geometry
		// to draw, and inventing a full-frame box would be a claim about WHERE the thing is.
		const [item] = itemsFromSelection(['a'], null, null, 'image', 'letter');

		expect(item!.prediction).toEqual([{ shape_type: 'tag', label: 'letter' }]);
	});

	it('puts the label on EVERY item, not just the first', () => {
		const items = itemsFromSelection(['a', 'b', 'c'], null, null, 'image', 'letter');

		expect(items.map((i) => i.prediction?.[0]?.label)).toEqual(['letter', 'letter', 'letter']);
	});

	it('omits `prediction` entirely when no label was chosen', () => {
		// Absent means "queue these blank, someone will draw them" — which is the ordinary send and
		// must stay byte-identical. An empty array would be a different statement.
		expect('prediction' in itemsFromSelection(['a'], null)[0]!).toBe(false);
	});

	it('SURVIVES the wire schema', () => {
		// The same class of defect as `dataset_version` above, and the reason that one is worth
		// pinning twice: valibot strips what it does not declare, in silence. A stripped prediction
		// means the bulk label posts 200, seeds every task, and carries nothing — the send looks
		// like it worked and every queued item is blank.
		const [item] = itemsFromSelection(['a'], 'vasa', 7, 'image', 'letter');

		const parsed = v.parse(SendItemSchema, item);

		expect(parsed.prediction).toEqual([{ shape_type: 'tag', label: 'letter' }]);
	});

	it('ignores a blank label rather than sending an empty class', () => {
		// `Select`'s unchosen value is `''` (a bindable with a fallback cannot be `undefined`), so
		// this is the shape the bar actually passes when nobody picked anything.
		expect('prediction' in itemsFromSelection(['a'], null, null, 'image', '')[0]!).toBe(false);
	});
});

describe('which classes a bulk label may offer', () => {
	it('offers a class that declares `tag`', () => {
		const choices = bulkLabelChoices({ classes: [{ name: 'letter', tools: ['tag'] }] });

		expect(choices).toEqual(['letter']);
	});

	it('offers an UNCONSTRAINED class — empty tools means any', () => {
		expect(bulkLabelChoices({ classes: [{ name: 'letter', tools: [] }] })).toEqual(['letter']);
	});

	it('WITHHOLDS a class that cannot be a whole-item tag', () => {
		// The server would refuse it (`membership_violation` checks the label WITH its primitive), so
		// offering it is offering a guaranteed 409. A class drawn only as a box is not something a
		// bulk classification can assert about a whole page.
		const choices = bulkLabelChoices({
			classes: [
				{ name: 'letter', tools: ['tag'] },
				{ name: 'stamp', tools: ['bbox'] },
			],
		});

		expect(choices).toEqual(['letter']);
	});

	it('offers nothing for an ontology with no classes', () => {
		expect(bulkLabelChoices({ classes: [] })).toEqual([]);
		expect(bulkLabelChoices(undefined)).toEqual([]);
	});
});
