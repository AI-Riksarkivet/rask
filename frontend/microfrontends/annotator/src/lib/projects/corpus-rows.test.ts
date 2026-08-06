/**
 * Joining a corpus row onto a queue row (#60).
 *
 * The fetch is not the dangerous part — the ASSOCIATION is. Rows come back unordered and possibly
 * short, and matching one to the wrong task puts one item's date on another item's row, silently,
 * looking exactly like data. These pin the association, not the transport.
 */

import { describe, expect, it } from 'vitest';

import { indexRows, keyString, rowFor, rowKeyFor, rowKeysFor, rowText } from './corpus-rows';

const task = (keys: string[]) => ({ source: { keys } });
const KEY_FIELDS = ['doc_id', 'speech_id', 'chunk_id'];

describe('turning a task key-path into the viewer address', () => {
	it('splits the doc key from the positional numeric rest', () => {
		expect(rowKeyFor(['vol-1', '3', '7'])).toEqual({ doc_id: 'vol-1', keys: [3, 7] });
	});

	it('refuses a NON-NUMERIC segment rather than sending NaN', () => {
		// NaN would render into the server's predicate and match nothing, while the request still
		// looked legitimate — an empty result that reads as "this row has no corpus data".
		expect(rowKeyFor(['vol-1', 'three'])).toBeNull();
	});

	it('refuses an empty key-path', () => {
		expect(rowKeyFor([])).toBeNull();
	});

	it('accepts a doc key with no rest (a document-level corpus)', () => {
		expect(rowKeyFor(['vol-1'])).toEqual({ doc_id: 'vol-1', keys: [] });
	});
});

describe('the keys a PAGE asks for', () => {
	it('asks once for a row two consensus replicas share', () => {
		// N replicas of one item carry the SAME key-path. Fetching per task would ask the viewer for
		// the same row N times on every page turn.
		const keys = rowKeysFor([task(['a', '1']), task(['a', '1']), task(['b', '2'])]);

		expect(keys).toHaveLength(2);
	});

	it('skips a task whose key-path cannot be addressed, without dropping the rest', () => {
		const keys = rowKeysFor([task(['a', '1']), task([]), task(['b', 'x']), task(['c', '3'])]);

		expect(keys).toEqual([
			{ doc_id: 'a', keys: [1] },
			{ doc_id: 'c', keys: [3] },
		]);
	});
});

describe('associating returned rows back to tasks', () => {
	it('matches by the row OWN key fields, not by return order', () => {
		// The endpoint documents that rows come back unordered. Trusting order is the bug this
		// avoids: reversed here, and both tasks must still get their own row.
		const rows = [
			{ doc_id: 'b', speech_id: 2, chunk_id: 0, year: 1912 },
			{ doc_id: 'a', speech_id: 1, chunk_id: 0, year: 1893 },
		];
		const index = indexRows(rows, KEY_FIELDS);

		expect(rowFor(task(['a', '1', '0']), index)?.year).toBe(1893);
		expect(rowFor(task(['b', '2', '0']), index)?.year).toBe(1912);
	});

	it('returns null for a task whose row did NOT come back', () => {
		// A task can legitimately outlive the row it points at (#37 made that a first-class state),
		// and a short response must read as "no corpus columns" rather than as an error.
		const index = indexRows([{ doc_id: 'a', speech_id: 1, chunk_id: 0 }], KEY_FIELDS);

		expect(rowFor(task(['gone', '9', '9']), index)).toBeNull();
	});

	it('does not index a row missing part of its own key', () => {
		// Half a key cannot address anything; indexing it under a partial string would let it match
		// a DIFFERENT task whose key-path happens to render the same way.
		const index = indexRows([{ doc_id: 'a', speech_id: null, chunk_id: 0 }], KEY_FIELDS);

		expect(index.size).toBe(0);
	});

	it('keeps two rows apart when a doc id contains the separator character', () => {
		// The collision the NUL separator exists to prevent. With '/' these two render identically.
		const a = keyString(['vol/1', '2']);
		const b = keyString(['vol', '1/2']);

		expect(a).not.toBe(b);
	});
});

describe('making the row reachable from the free-text filter', () => {
	it('includes the row values, which is what "filter to the 1890s" needs', () => {
		const text = rowText({ doc_id: 'a', speech_id: 1, year: 1893, title: 'Riksdag' }, KEY_FIELDS);

		expect(text).toContain('1893');
		expect(text).toContain('Riksdag');
	});

	it('EXCLUDES the key fields, so a doc-id search does not match every field on that row', () => {
		const text = rowText({ doc_id: 'vol-1', speech_id: 1, chunk_id: 0, year: 1893 }, KEY_FIELDS);

		expect(text).not.toContain('vol-1');
	});

	it('skips nested values rather than stringifying them into noise', () => {
		// `alignments` rides on every hit and would otherwise dump "[object Object]" — or worse, a
		// whole serialized array — into the searchable text of every row.
		const text = rowText({ doc_id: 'a', alignments: [{ w: 'x' }], year: 1893 }, KEY_FIELDS);

		expect(text.trim()).toBe('1893');
	});

	it('is empty for a task with no row, never the string "null"', () => {
		expect(rowText(null, KEY_FIELDS)).toBe('');
	});
});
