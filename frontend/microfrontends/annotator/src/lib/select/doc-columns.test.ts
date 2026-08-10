/**
 * The corpus browser's document columns.
 *
 * Reported: "why does browse corpus looks so fucking wierd? why does it show like gallery view
 * initally?" — a surface whose row is now called "Bulk labeling" opened on a gallery of one-document
 * tiles, with nothing to sort or filter by.
 */

import { describe, expect, it } from 'vitest';

import {
	compareDocs,
	docCellText,
	docColumns,
	DOC_ID,
	DOC_TITLE,
	matchesDoc,
	type DocView,
} from './doc-columns';

const view: DocView = {
	docId: (r) => String(r.doc_id ?? ''),
	// `title` + `titleFields`, mirroring the REAL `DatasetView`. The first fixture invented
	// `docTitle`, which no corpus has — so these tests passed while the column could never render.
	title: (r) => String(r.label ?? r.doc_id ?? ''),
	titleFields: ['label'],
	metadataFields: [
		{ field: 'year', label: 'Year' },
		{ field: 'archive', label: 'Archive' },
	],
};

const rows: Record<string, unknown>[] = [
	{
		doc_id: 'bind86',
		label: 'Landshandlingar 1663',
		year: 1663,
		archive: 'RA',
		subjects: ['tax', 'land'],
	},
	{ doc_id: 'bind9', label: 'Vasa letters', year: 9, archive: 'ÅA' },
	{ doc_id: 'bind12', label: null, year: null, archive: 'RA' },
];

describe('which columns a document table shows', () => {
	it('leads with IDENTITY, then whatever the corpus declares', () => {
		// The mirrored form of #58's complaint: a queue that showed none of the item's own columns was
		// the bug, and a table of pure metadata with no way to tell WHICH document a row is would be
		// the same bug facing the other way.
		expect(docColumns(view).map((c) => c.field)).toEqual([DOC_ID, DOC_TITLE, 'year', 'archive']);
	});

	it('drops the title column when the corpus declares no title', () => {
		// A column empty for every row costs width and implies the data is missing rather than
		// undeclared.
		const untitled: DocView = { ...view, titleFields: [] };

		expect(docColumns(untitled).map((c) => c.field)).toEqual([DOC_ID, 'year', 'archive']);
	});

	it('keeps the DECLARED order, not alphabetical', () => {
		// The descriptor's author chose it. Re-sorting would silently overrule a decision made where
		// the corpus is defined.
		const reversed: DocView = {
			...view,
			metadataFields: [
				{ field: 'zzz', label: 'Z' },
				{ field: 'aaa', label: 'A' },
			],
		};

		expect(docColumns(reversed).map((c) => c.field)).toEqual([DOC_ID, DOC_TITLE, 'zzz', 'aaa']);
	});
});

describe('cell text', () => {
	it('resolves the synthetic identity columns through the VIEW, not the row', () => {
		// `doc_id` is the descriptor's choice of key field; a table that read a hardcoded `row.id`
		// would be blank on every corpus that names it anything else.
		expect(docCellText(view, rows[0]!, DOC_ID)).toBe('bind86');
		expect(docCellText(view, rows[0]!, DOC_TITLE)).toBe('Landshandlingar 1663');
	});

	it('renders a repeated field joined, not as [object Object]', () => {
		// Archival metadata is full of repeated fields (authors, subjects). `String(value)` on an
		// array is the case a naive implementation gets visibly wrong.
		expect(docCellText(view, rows[0]!, 'subjects')).toBe('tax, land');
	});

	it('renders a missing value as EMPTY, never as "null"', () => {
		expect(docCellText(view, rows[2]!, 'year')).toBe('');
	});

	it('falls back to the DOC ID when a row has no title value', () => {
		// The real `DatasetView.title` does this — "first non-empty declared title field, else the doc
		// id" — so the column shows the id rather than a hole for a row whose title happens to be
		// null. That is a per-ROW fallback and is different from the corpus declaring no title at all,
		// which drops the column entirely (above).
		expect(docCellText(view, rows[2]!, DOC_TITLE)).toBe('bind12');
	});
});

describe('the free-text filter', () => {
	it('matches across EVERY displayed column, not just the title', () => {
		// Someone typing `1663` means the year wherever it lives. Restricting to one column answers a
		// question nobody asked.
		expect(matchesDoc(view, rows[0]!, '1663')).toBe(true);
		expect(matchesDoc(view, rows[0]!, 'RA')).toBe(true);
		expect(matchesDoc(view, rows[0]!, 'bind86')).toBe(true);
	});

	it('is case-insensitive and ignores surrounding space', () => {
		expect(matchesDoc(view, rows[1]!, '  vasa ')).toBe(true);
	});

	it('matches everything on an empty query', () => {
		expect(rows.every((r) => matchesDoc(view, r, ''))).toBe(true);
	});

	it('does NOT match on an undeclared field', () => {
		// `subjects` is real data but not a declared column, so it is not on screen — filtering on it
		// would hide rows for a reason the person cannot see.
		expect(matchesDoc(view, rows[0]!, 'tax')).toBe(false);
	});
});

describe('sorting', () => {
	it('compares numerically when both sides are numbers', () => {
		// The defect a string sort ships: `1663` before `9` because "1" < "9".
		expect(compareDocs(view, rows[1]!, rows[0]!, 'year')).toBeLessThan(0);
	});

	it('falls back to a LOCALE compare for text', () => {
		// `Å` belongs after `Z` for a Swedish reader, which matters for this archive.
		expect(compareDocs(view, rows[0]!, rows[1]!, 'archive')).toBeLessThan(0);
	});

	it('does not treat a MIXED pair as numeric', () => {
		// One numeric side and one empty must not become `NaN - n`, which sorts unpredictably.
		expect(Number.isNaN(compareDocs(view, rows[0]!, rows[2]!, 'year'))).toBe(false);
	});
});
