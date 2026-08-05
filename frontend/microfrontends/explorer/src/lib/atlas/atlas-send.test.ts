/**
 * Turning a map selection into a labelling batch.
 *
 * The atlas could always lasso, and the selection dead-ended in a results table. These pin the rules
 * that decide what a project actually receives from a region of the map.
 *
 * The one that matters most is the partial-selection refusal. The atlas fetches selection detail
 * CAPPED while reporting the TRUE selection size, so a big lasso leaves those two numbers disagreeing
 * — and sending the fetched half while the screen says five thousand is the worst available failure,
 * because it reports success.
 */

import { describe, expect, it } from 'vitest';

import { predictionFor, SEND_TASK_CAP, selectionKeys, sendRefusal } from './atlas-send';

describe('the label a region gets', () => {
	it('is ONE whole-item tag', () => {
		// Bulk classification has no geometry to draw, and a fabricated full-frame box would be a
		// claim about WHERE the thing is.
		expect(predictionFor('letter')).toEqual([{ shape_type: 'tag', label: 'letter' }]);
	});

	it('is ABSENT when no label was chosen, rather than an empty list', () => {
		// Absent means "queue this region blank for someone to draw on" — the ordinary send, which
		// must keep working byte-identically.
		expect(predictionFor('')).toBeUndefined();
		expect(predictionFor('   ')).toBeUndefined();
	});

	it('trims, so a stray space does not become a different class', () => {
		expect(predictionFor(' letter ')).toEqual([{ shape_type: 'tag', label: 'letter' }]);
	});

	it('carries NO source — the server stamps it', () => {
		// A sender able to write `source` could mark items nobody looked at as human work.
		expect(Object.keys(predictionFor('letter')![0]!)).toEqual(['shape_type', 'label']);
	});
});

describe('when a selection cannot be sent', () => {
	it('refuses an empty selection', () => {
		expect(sendRefusal({ fetched: 0, total: 0 })).toMatch(/lasso a region/i);
	});

	it('refuses a selection larger than the server accepts, and NAMES the cap', () => {
		// FULLY LOADED and over the cap, so only the cap rule can produce this refusal. The obvious
		// fixture — fetched 1000 of total 5000 — falls through to the partial-load rule, whose message
		// happens to contain both numbers too; the assertion then passed with the cap check deleted.
		// A mutation caught that. An assertion that holds without the code it tests is not a test.
		const reason = sendRefusal({ fetched: 1500, total: 1500 });

		expect(reason).toContain('1,500');
		expect(reason).toContain(SEND_TASK_CAP.toLocaleString());
		expect(reason).toMatch(/exceeds/i);
		expect(reason).not.toMatch(/loaded/i);
	});

	it('REFUSES when fewer items have loaded than are selected', () => {
		// The defect this exists for. Sending the fetched subset would queue a fraction of what was
		// lassoed and report success, while the count on screen said otherwise. Checked separately
		// from the cap so the two can be changed independently without silently truncating.
		const reason = sendRefusal({ fetched: 400, total: 900, cap: 1000 });

		expect(reason).toContain('400');
		expect(reason).toContain('900');
		expect(reason).toMatch(/loaded/i);
	});

	it('refuses with no project chosen', () => {
		expect(sendRefusal({ fetched: 5, total: 5, projectChosen: false })).toMatch(
			/choose a labeling task/i,
		);
	});

	it('allows a fully-loaded selection within the cap', () => {
		expect(sendRefusal({ fetched: 300, total: 300 })).toBeNull();
	});

	it('allows a selection with NO label — labelling is optional', () => {
		// "Queue this region for someone to draw on" is the ordinary send and is not a refusal.
		expect(sendRefusal({ fetched: 10, total: 10 })).toBeNull();
	});

	it('allows exactly the cap', () => {
		expect(sendRefusal({ fetched: SEND_TASK_CAP, total: SEND_TASK_CAP })).toBeNull();
	});
});

describe('the keys a selection produces', () => {
	const keyPath = (h: { doc: string; n: number }) => [h.doc, String(h.n)];

	it('joins the descriptor key path, like every other key in the estate', () => {
		expect(selectionKeys([{ doc: 'd1', n: 2 }], keyPath)).toEqual(['d1/2']);
	});

	it('de-duplicates, because two tasks for one item is two people labelling it', () => {
		const hits = [
			{ doc: 'd1', n: 2 },
			{ doc: 'd1', n: 2 },
			{ doc: 'd1', n: 3 },
		];

		expect(selectionKeys(hits, keyPath)).toEqual(['d1/2', 'd1/3']);
	});

	it('drops a hit whose key path is empty rather than sending a blank key', () => {
		expect(selectionKeys([{ doc: '', n: 0 }], () => [])).toEqual([]);
	});

	it('uses the CALLER descriptor, so the encoding matches the rest of the estate', () => {
		// The descriptor's own keyPath percent-encodes each part. Passing it in rather than
		// re-implementing here is what stops this producing a key that looks right and resolves
		// against nothing.
		const encoded = (h: { doc: string; n: number }) => [encodeURIComponent(h.doc), String(h.n)];

		expect(selectionKeys([{ doc: 'a b', n: 1 }], encoded)).toEqual(['a%20b/1']);
	});
});
