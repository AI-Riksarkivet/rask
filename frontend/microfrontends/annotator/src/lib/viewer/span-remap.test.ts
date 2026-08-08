/**
 * Span offsets FOLLOW the text they annotate (§8d.1). Before this existed, correcting one typo
 * at the start of a line silently re-pointed every entity after it at the wrong characters —
 * annotations corrupted by the act of improving the data they annotate.
 */

import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it, vi } from 'vitest';

import { editedRegion, remapSpans } from './span-remap';

vi.mock('svelte-sonner', () => ({
	toast: {
		success: () => {},
		error: () => {},
		message: () => {},
		warning: () => {},
		info: () => {},
	},
}));
vi.mock('./remote/assist.remote', () => ({
	requestAssist: async () => ({ ok: true, data: { shapes: [], source: 's', dropped: [] } }),
	assistProducers: async () => [],
}));
vi.mock('./remote/jobs.remote', () => ({
	submitBatchJob: async () => ({
		ok: true,
		data: { job_id: 'j', status: 'queued', backend: 'mock' },
	}),
	jobStatus: async () => null,
}));

import { AnnotatorController } from './annotator.svelte.js';

// 'Gustaf Eriksson Vasa reste till Dalarna' — span 'Vasa' at [16, 20), 'Dalarna' at [32, 39).
const TEXT = 'Gustaf Eriksson Vasa reste till Dalarna';

describe('editedRegion', () => {
	it('recovers the single replaced region from prefix + suffix', () => {
		// 'Eriksson' → 'Erikson': one char deleted inside.
		expect(editedRegion(TEXT, TEXT.replace('Eriksson', 'Erikson'))).toMatchObject({ from: 12 });
	});

	it('equal strings yield an empty region', () => {
		const r = editedRegion(TEXT, TEXT);
		expect(r.from).toBe(r.toOld);
		expect(r.toOld).toBe(r.toNew);
	});
});

describe('remapSpans', () => {
	const SPANS = [
		{ index: 1, start: 16, end: 20 }, // 'Vasa'
		{ index: 2, start: 32, end: 39 }, // 'Dalarna'
	];

	it('an edit BEFORE a span shifts it; an edit AFTER leaves it alone', () => {
		// 'Eriksson' → 'Erikssonn' (+1 char at offset ~15): both spans sit after → both shift.
		const grown = TEXT.replace('Eriksson', 'Erikssonn');
		expect(remapSpans(TEXT, grown, SPANS)).toEqual([
			{ index: 1, start: 17, end: 21 },
			{ index: 2, start: 33, end: 40 },
		]);
		// Append at the very end: nothing before it moves.
		expect(remapSpans(TEXT, `${TEXT} 1520`, SPANS)).toEqual(SPANS);
	});

	it('an edit INSIDE a span stretches the span with its text', () => {
		// 'Dalarna' → 'Dalecarlia' (+3): the span grows, start pinned.
		const edited = TEXT.replace('Dalarna', 'Dalecarlia');
		const [, dalarna] = remapSpans(TEXT, edited, SPANS);
		expect(dalarna).toEqual({ index: 2, start: 32, end: 42 });
	});

	it('a span whose anchored text is PARTIALLY edited away is dropped, never guessed', () => {
		// Delete 'Vasa reste' — the Vasa span's text is gone; Dalarna shifts.
		const cut = TEXT.replace('Vasa reste till ', '');
		const [vasa, dalarna] = remapSpans(TEXT, cut, SPANS);
		expect(vasa).toEqual({ index: 1, start: null, end: null });
		expect(dalarna).toEqual({ index: 2, start: 16, end: 23 });
	});
});

describe('the controller applies the remap on a transcription edit', () => {
	function harness() {
		const noop = () => {};
		return {
			plugins: {
				arrow: new Proxy(
					{ getDirtyOverrides: () => new Map(), isDeleted: () => false },
					{ get: (t: Record<string, unknown>, k: string) => t[k] ?? noop },
				),
				interaction: new Proxy(
					{ getSelectedSet: () => new Set<number>(), cvCapable: false },
					{ get: (t: Record<string, unknown>, k: string) => t[k] ?? noop, set: () => true },
				),
				image: { onViewportChange: undefined, zoomPercent: 100 },
			},
		};
	}

	const TABLE = tableFromArrays({
		id: ['line', 'vasa', 'dala'],
		shape_type: ['text', 'text', 'text'],
		label: ['', 'person', 'place'],
		text: [TEXT, '', ''],
		parent_id: ['', 'line', 'line'],
		char_start: Int32Array.from([-1, 16, 32]),
		char_end: Int32Array.from([-1, 20, 39]),
		status: ['accepted', 'accepted', 'accepted'],
		source: ['ingest', 'human', 'human'],
	});

	it('spans shift in the rendered rows AND ride the Save payload as span edits', () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE, '/annotator/api/annotations/d/0/1');

		c.updateField(0, 'text', TEXT.replace('Eriksson', 'Erikssonn'));

		const byId = Object.fromEntries(c.rows.map((r) => [r.id, r]));
		expect([byId['vasa']!.charStart, byId['vasa']!.charEnd]).toEqual([17, 21]);
		expect([byId['dala']!.charStart, byId['dala']!.charEnd]).toEqual([33, 40]);
		expect(c.dirty).toBe(true);
	});

	it('a span edited away is DELETED, not left pointing at the wrong characters', () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE, '/annotator/api/annotations/d/0/1');

		c.updateField(0, 'text', TEXT.replace('Vasa reste till ', ''));

		const ids = c.rows.map((r) => r.id);
		expect(ids).not.toContain('vasa');
		// The surviving span followed the shift.
		const dala = c.rows.find((r) => r.id === 'dala')!;
		expect([dala.charStart, dala.charEnd]).toEqual([16, 23]);
	});
});
