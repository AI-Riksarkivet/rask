/**
 * A hit is rendered through ITS OWN corpus's view.
 *
 * Every renderer read `activeView()`, which is correct exactly while one corpus fills the screen. It
 * stops being correct the moment a fused result set carries rows from two corpora: display fields,
 * media kind and capabilities are all per-corpus, so every row would be described as if it came from
 * whichever corpus happened to be selected — and it still renders, so nothing complains.
 *
 * The search service stamps `_dataset` on every hit. This is the half that reads it.
 */

import { describe, expect, it } from 'vitest';
import * as v from 'valibot';

import {
	DatasetDescriptorSchema,
	DatasetView,
	registerView,
	setActiveView,
	viewForHit,
	type Row,
} from './descriptor';

function view(id: string, mime: string): DatasetView {
	return new DatasetView(
		v.parse(DatasetDescriptorSchema, {
			id,
			tables: {
				items: {
					name: 'items',
					row_count: 1,
					version: 1,
					columns: [{ name: 'item_id', arrow_type: 'string', nullable: false }],
					indexes: [],
				},
			},
			declared: {
				identity: { key_fields: ['item_id'], doc_key: 'item_id', doc_key_pattern: '.*' },
				document: { table: 'items', media_blob: 'blob', mime },
				time: null,
				display: { title: ['label'], body: 'caption', caption: null, metadata: [] },
				search: { row_table: 'items', fts: null, vectors: {}, filterable: [], rerank: false },
				atlas: [],
				capabilities: {},
			},
		}),
	);
}

const hit = (dataset?: string): Row =>
	({ item_id: 'x', ...(dataset ? { _dataset: dataset } : {}) }) as Row;

describe('viewForHit', () => {
	it('resolves the view the hit NAMES, not the active one', () => {
		// The defect, stated directly: with `pages` active, an audio hit from `voices` would have been
		// described as an image.
		setActiveView(view('pages', 'image/png'));
		registerView(view('voices', 'audio/wav'));

		expect(viewForHit(hit('voices')).mediaKind).toBe('audio');
		expect(viewForHit(hit('pages')).mediaKind).toBe('image');
	});

	it('renders two corpora correctly in ONE list', () => {
		// The case the whole change exists for.
		setActiveView(view('pages', 'image/png'));
		registerView(view('voices', 'audio/wav'));

		const kinds = [hit('pages'), hit('voices'), hit('pages')].map((h) => viewForHit(h).mediaKind);

		expect(kinds).toEqual(['image', 'audio', 'image']);
	});

	it('falls back to the ACTIVE view for a hit that names no corpus', () => {
		// Not a guess: it reproduces the previous behaviour exactly for the single-corpus case, which
		// is the case where reading the active view was right.
		setActiveView(view('pages', 'image/png'));

		expect(viewForHit(hit()).id).toBe('pages');
	});

	it('falls back to the ACTIVE view for a corpus that was never loaded', () => {
		// The honest floor. Throwing mid-render would surface a failed descriptor fetch on a list that
		// is otherwise fine, and a hit is not where that should be discovered.
		setActiveView(view('pages', 'image/png'));

		expect(viewForHit(hit('never-loaded')).id).toBe('pages');
	});

	it('setActiveView also REGISTERS, so the active corpus resolves by name', () => {
		// Otherwise the commonest hit of all — one from the corpus you are looking at — would take the
		// fallback path and only work by accident.
		setActiveView(view('pages', 'image/png'));

		expect(viewForHit(hit('pages')).id).toBe('pages');
	});

	it('a later registration REPLACES an earlier one for the same id', () => {
		// A corpus reloaded after a schema change must not keep answering from the stale descriptor.
		setActiveView(view('pages', 'image/png'));
		registerView(view('voices', 'audio/wav'));
		registerView(view('voices', 'video/mp4'));

		expect(viewForHit(hit('voices')).mediaKind).toBe('video');
	});
});
