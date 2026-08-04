import { describe, expect, it } from 'vitest';
import { tableFromIPC } from 'apache-arrow';
import { JSON_COLUMNS_KEY, rowsToArrowIPC } from '$lib/server/rows-arrow';

/**
 * The encoder half of the two Arrow promotions (`api/search`, `api/atlas/chunks`).
 *
 * A corpus row is a LOOSE object — `RowSchema` names only the ranking signals and the alignments blob,
 * and every other column belongs to whatever dataset is mounted. So the interesting cases are all about
 * a schema nobody declared: a key that appears only on a later row, a column that is null for some rows,
 * a nested value, and a column whose values genuinely disagree about their type. Decoding here mirrors
 * `rowsFromArrow` in `@rask/explorer-api` exactly, including the rule that decides whether a hit parses at
 * all: NULL CELLS ARE OMITTED, because `RowSchema`'s optional fields admit `undefined` and reject `null`.
 */
function decode(bytes: Uint8Array): Record<string, unknown>[] {
	const table = tableFromIPC(bytes);
	const jsonColumns = new Set<string>(
		JSON.parse(table.schema.metadata.get(JSON_COLUMNS_KEY) ?? '[]') as string[],
	);
	const names = table.schema.fields.map((f) => f.name);
	const rows: Record<string, unknown>[] = [];
	for (const record of table) {
		const row: Record<string, unknown> = {};
		for (const name of names) {
			const value: unknown = record[name];
			if (value === null || value === undefined) continue;
			row[name] = jsonColumns.has(name) ? JSON.parse(String(value)) : value;
		}
		rows.push(row);
	}
	return rows;
}

describe('rowsToArrowIPC', () => {
	it('round-trips a search hit, nested blob and all', () => {
		const hit = {
			doc_id: 'd1',
			speech_id: 0,
			chunk_id: 0,
			title: 'Hello world',
			text: 'the quick brown fox jumps',
			_score: 3.2,
			alignments: [{ start: 1, end: 2, text: 'fox' }],
		};

		expect(decode(rowsToArrowIPC([hit]))).toEqual([hit]);
	});

	it('a key absent from a row stays ABSENT, never null', () => {
		// The rule the whole decode hangs on: `_score` is `v.optional(v.number())`, so a hit that
		// arrives with `_score: null` fails the parse — every result of a non-FTS search, in other words.
		const rows = decode(
			rowsToArrowIPC([{ doc_id: 'd1', _score: 3.2 }, { doc_id: 'd2' }] as Record<
				string,
				unknown
			>[]),
		);

		expect(rows[1]).toEqual({ doc_id: 'd2' });
		expect('_score' in (rows[1] ?? {})).toBe(false);
	});

	it('a column that only later rows carry is still a column', () => {
		const rows = decode(rowsToArrowIPC([{ doc_id: 'd1' }, { doc_id: 'd2', speaker: 'ada' }]));

		expect(rows).toEqual([{ doc_id: 'd1' }, { doc_id: 'd2', speaker: 'ada' }]);
	});

	it('a column whose values disagree about their type rides as JSON text', () => {
		// Not hypothetical: a corpus column can be a number on one row and a string on the next, and a
		// single Arrow type for it would either throw or silently coerce. Both survive as themselves.
		const rows = decode(rowsToArrowIPC([{ mixed: 1 }, { mixed: 'one' }, { mixed: true }]));

		expect(rows).toEqual([{ mixed: 1 }, { mixed: 'one' }, { mixed: true }]);
	});

	it('an all-null column decodes as an absent field rather than a guessed type', () => {
		expect(decode(rowsToArrowIPC([{ doc_id: 'd1', tags: null }]))).toEqual([{ doc_id: 'd1' }]);
	});

	it('an empty result is an empty table, not a malformed payload', () => {
		const bytes = rowsToArrowIPC([]);

		expect(decode(bytes)).toEqual([]);
		expect(tableFromIPC(bytes).schema.metadata.get('rows')).toBe('0');
	});
});
