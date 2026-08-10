/**
 * Compare-view logic — preference as ordinary rows, version-guarded per pane.
 *
 * The three rules the view depends on: a pane's active choices are read from its SAVED table
 * (item-level `tag` rows only — drawn shapes are not choices), a chip toggle is exactly one
 * insert OR one delete against the version the pane was read at, and a text pane's body is the
 * same reading order the transcription lane uses.
 */

import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it } from 'vitest';

import { paneOrdinal, paneTags, paneTextLines, toggleTagPayload } from './compare';

const TABLE = tableFromArrays({
	id: ['t1', 's1'],
	shape_type: ['tag', 'bbox'],
	label: ['preferred', 'stamp'],
	status: ['accepted', 'accepted'],
	source: ['human', 'human'],
});

describe('compare view', () => {
	it('paneTags reads item-level tag rows only — a drawn shape is not a choice', () => {
		expect(paneTags(TABLE)).toEqual([{ id: 't1', label: 'preferred' }]);
	});

	it('toggling an ABSENT choice is one insert, pinned to the read version', () => {
		const p = toggleTagPayload(paneTags(TABLE), 'best-scan', 7);

		expect(p.deletes).toEqual([]);
		expect(p.inserts).toHaveLength(1);
		expect(p.inserts[0]).toMatchObject({
			shape_type: 'tag',
			label: 'best-scan',
			status: 'accepted',
			source: 'human',
		});
		expect(p.base_version).toBe(7);
	});

	it('toggling a PRESENT choice is one delete of that row, nothing else', () => {
		const p = toggleTagPayload(paneTags(TABLE), 'preferred', 7);

		expect(p.inserts).toEqual([]);
		expect(p.deletes).toEqual(['t1']);
	});

	it('a text pane reads like the transcription lane — text rows, spans excluded', () => {
		const table = tableFromArrays({
			id: ['l1', 'sp1'],
			shape_type: ['text', 'text'],
			text: ['första raden', 'rst'],
			parent_id: ['', 'l1'],
			label: ['', 'damaged'],
			status: ['accepted', 'accepted'],
			source: ['ingest', 'human'],
		});

		expect(paneTextLines(table).map((l) => l.text)).toEqual(['första raden']);
	});

	it('panes are candidates, not pages: A, B, C…', () => {
		expect([0, 1, 2].map(paneOrdinal)).toEqual(['A', 'B', 'C']);
	});
});
