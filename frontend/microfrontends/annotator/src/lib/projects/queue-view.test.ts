/**
 * The queue as a GRID.
 *
 * A list is how you audit a queue; a grid is how you scan one. For page images the grid answers
 * "which of these are letters" in a glance, and rask had list only — so judging forty items meant
 * reading forty rows of keys.
 *
 * The thumbnail URL is the part worth pinning: assembled differently from `DatasetView.frameUrl` it
 * would 404 against the very cache the browse grid already fills, and every tile would render as a
 * broken image with nothing saying why.
 */

import { describe, expect, it } from 'vitest';

import { cardCaption, parseView, taskImageUrl } from './queue-view';
import type { TaskDetail } from './types';

const api = (path: string) => `/annotator${path}`;

function task(over: Record<string, unknown> = {}): TaskDetail {
	return {
		task_id: 't1',
		project_id: 'p1',
		state: 'unassigned',
		source: { kind: 'chunks', keys: ['doc-a/1/12'], where: null },
		media: { kind: 'image', image_url: null, media_url: null },
		prediction: [],
		review_notes: [],
		legal_events: [],
		...over,
	} as unknown as TaskDetail;
}

describe('which view mode is in force', () => {
	it('reads a stored grid preference', () => {
		expect(parseView('grid')).toBe('grid');
	});

	it('falls back to LIST for anything unrecognised', () => {
		// List works for every corpus, including ones with no imagery to put in a grid. A stored value
		// from a future mode must not leave the queue rendering nothing.
		expect(parseView(null)).toBe('list');
		expect(parseView('')).toBe('list');
		expect(parseView('mosaic')).toBe('list');
	});
});

describe('the tile image', () => {
	it('builds the viewer frame URL from the item KEY', () => {
		// The same construction `DatasetView.frameUrl` uses. Assembled differently it 404s against the
		// cache the browse grid already populates, and every tile breaks.
		expect(taskImageUrl(task(), api)).toBe('/annotator/api/chunk-frame/doc-a/1/12');
	});

	it('carries the DATASET, so a non-default corpus resolves', () => {
		const t = task({ source: { kind: 'chunks', keys: ['doc-a/1/12'], where: 'vasa' } });

		expect(taskImageUrl(t, api)).toBe('/annotator/api/chunk-frame/doc-a/1/12?dataset=vasa');
	});

	it('omits the dataset query for the default corpus', () => {
		// Not `?dataset=` with an empty value, which the viewer would read as a corpus named "".
		expect(taskImageUrl(task(), api)).not.toContain('?dataset');
	});

	it('encodes a dataset name that needs it', () => {
		const t = task({ source: { kind: 'chunks', keys: ['k/1/1'], where: 'a b' } });

		expect(taskImageUrl(t, api)).toContain('dataset=a%20b');
	});

	it('PREFERS an explicitly supplied image_url', () => {
		// No sender sets it today, but it is on the wire and honouring it costs one line.
		const t = task({ media: { kind: 'image', image_url: 'https://cdn/x.jpg', media_url: null } });

		expect(taskImageUrl(t, api)).toBe('https://cdn/x.jpg');
	});

	it('returns NULL when the item has no key, rather than a broken URL', () => {
		// An empty tile reads as "no preview"; a broken <img> reads as "this item failed".
		const t = task({ source: { kind: 'scope', keys: [], where: null } });

		expect(taskImageUrl(t, api)).toBeNull();
	});

	it('uses the FIRST key of a multi-key item', () => {
		const t = task({ source: { kind: 'chunks', keys: ['a/1/1', 'a/1/2'], where: null } });

		expect(taskImageUrl(t, api)).toBe('/annotator/api/chunk-frame/a/1/1');
	});
});

describe('the tile caption', () => {
	it('drops the document part, which is identical across the grid', () => {
		// `fe00cd746463ad2c/1/12` is unreadable at card size, and the leading hash is the same on
		// every tile of a document — so it carries no information where it costs the most room.
		expect(cardCaption(task())).toBe('1/12');
	});

	it('keeps a single-part key whole', () => {
		expect(cardCaption(task({ source: { kind: 'chunks', keys: ['solo'], where: null } }))).toBe(
			'solo',
		);
	});

	it('falls back to the task id when there is no key at all', () => {
		const t = task({ source: { kind: 'scope', keys: [], where: null } });

		expect(cardCaption(t)).toBe('t1');
	});
});
