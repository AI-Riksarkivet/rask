/**
 * Choosing a corpus.
 *
 * The reported problem was "no way to CHOOSE a dataset". The MECHANISM was never missing —
 * `descriptor-store` has always read `?dataset=<id>`, and the backend has always listed the corpora
 * at `GET /api/datasets`. What was missing was anything on screen that said so, so picking one meant
 * knowing the mechanism and hand-editing the URL.
 *
 * These cover the two rules that make the picker correct rather than merely present: which entry is
 * marked current, and what URL each entry links to.
 */

import { describe, expect, it } from 'vitest';

import { datasetChoices, datasetHref, type DatasetSummaryLike } from './dataset-choice';

const summary = (id: string, rows = 10, capabilities: string[] = []): DatasetSummaryLike => ({
	id,
	tables: { chunks: { row_count: rows } },
	capabilities,
});

describe('the entries', () => {
	it('sums rows across every table in the corpus', () => {
		const [choice] = datasetChoices(
			[
				{
					id: 'a',
					tables: { chunks: { row_count: 3 }, pages: { row_count: 4 } },
					capabilities: [],
				},
			],
			null,
			null,
		);

		expect(choice?.rows).toBe(7);
	});

	it('marks the ACTIVE corpus, which is what the descriptor loaded', () => {
		const choices = datasetChoices([summary('a'), summary('b')], 'b', null);

		expect(choices.map((c) => [c.id, c.active])).toEqual([
			['a', false],
			['b', true],
		]);
	});

	it('marks the active one even when it IS the default', () => {
		// The normal case, and the one an implementation keyed off the query param would get wrong:
		// with no `?dataset=` the store derives the default corpus's id from health, so a picker
		// reading the param would show nothing selected on the page everyone lands on first.
		const choices = datasetChoices([summary('demo'), summary('other')], 'demo', 'demo');

		expect(choices.find((c) => c.id === 'demo')?.active).toBe(true);
		expect(choices.find((c) => c.id === 'demo')?.isDefault).toBe(true);
	});

	it('orders by id so the menu does not reshuffle between opens', () => {
		expect(
			datasetChoices([summary('c'), summary('a'), summary('b')], null, null).map((c) => c.id),
		).toEqual(['a', 'b', 'c']);
	});

	it('carries capabilities, sorted, so two corpora read comparably', () => {
		const [choice] = datasetChoices([summary('a', 1, ['topics', 'frames'])], null, null);

		expect(choice?.capabilities).toEqual(['frames', 'topics']);
	});

	it('is empty when the backend serves nothing', () => {
		expect(datasetChoices([], null, null)).toEqual([]);
	});
});

describe('the href each entry links to', () => {
	const at = (path: string) => new URL(`http://x${path}`);

	it('sets ?dataset= for a non-default corpus', () => {
		expect(datasetHref(at('/explorer/'), { id: 'smoke', isDefault: false })).toBe(
			'/explorer/?dataset=smoke',
		);
	});

	it('DROPS the param entirely for the default corpus', () => {
		// `?dataset=<default-id>` works, but it makes the plain URL and the explicit one two different
		// strings for one place — and only the plain one is what the zone links to internally.
		expect(datasetHref(at('/explorer/?dataset=smoke'), { id: 'demo', isDefault: true })).toBe(
			'/explorer/',
		);
	});

	it('preserves every OTHER param', () => {
		// The search page keeps its query, mode and filters in the URL. Rebuilding the address from
		// scratch would silently discard the search someone is in the middle of.
		const href = datasetHref(at('/explorer/?q=vasa&mode=hybrid'), {
			id: 'smoke',
			isDefault: false,
		});

		const params = new URL(`http://x${href}`).searchParams;
		expect(params.get('q')).toBe('vasa');
		expect(params.get('mode')).toBe('hybrid');
		expect(params.get('dataset')).toBe('smoke');
	});

	it('REPLACES an existing dataset param rather than appending a second', () => {
		expect(datasetHref(at('/explorer/?dataset=a'), { id: 'b', isDefault: false })).toBe(
			'/explorer/?dataset=b',
		);
	});

	it('stays on the current route, not just the zone root', () => {
		// Switching corpora from /atlas should keep you on /atlas.
		expect(datasetHref(at('/explorer/atlas'), { id: 'smoke', isDefault: false })).toBe(
			'/explorer/atlas?dataset=smoke',
		);
	});
});
