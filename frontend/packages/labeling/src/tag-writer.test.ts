import { describe, expect, it } from 'vitest';
import { tagBatchFromTaggedHits, tagRemovesFromEntries } from './tag-writer';

// The identity key_fields for a transcripts-shaped descriptor (doc + 2 numeric keys).
const KEY_FIELDS = ['doc_id', 'speech_id', 'chunk_id'];

describe('tagBatchFromTaggedHits', () => {
	it('maps tagged rows to per-row identity, doc_key first + rest coerced to numbers', () => {
		const rows = [
			{ doc_id: 'd1', speech_id: 0, chunk_id: 19, text: '…' },
			{ doc_id: 'd1', speech_id: 0, chunk_id: 20, text: '…' },
		];
		const tags: Record<string, string[]> = {
			'd1|0|19': ['speech', 'music'],
			'd1|0|20': ['applause'],
		};
		const batch = tagBatchFromTaggedHits(
			rows,
			KEY_FIELDS,
			(r) => tags[`${r.doc_id}|${r.speech_id}|${r.chunk_id}`] ?? [],
		);
		expect(batch.adds).toEqual([
			{ doc_id: 'd1', keys: [0, 19], labels: ['speech', 'music'] },
			{ doc_id: 'd1', keys: [0, 20], labels: ['applause'] },
		]);
	});

	it('skips untagged rows (no empty writes)', () => {
		const rows = [
			{ doc_id: 'd1', speech_id: 0, chunk_id: 19 },
			{ doc_id: 'd1', speech_id: 0, chunk_id: 20 },
		];
		const batch = tagBatchFromTaggedHits(rows, KEY_FIELDS, (r) =>
			r.chunk_id === 19 ? ['speech'] : [],
		);
		expect(batch.adds).toEqual([{ doc_id: 'd1', keys: [0, 19], labels: ['speech'] }]);
	});

	it('is arity-generic — a single-field identity yields no extra keys', () => {
		const batch = tagBatchFromTaggedHits([{ page_id: 'p7' }], ['page_id'], () => ['figure']);
		expect(batch.adds).toEqual([{ doc_id: 'p7', keys: [], labels: ['figure'] }]);
	});
});

describe('tagRemovesFromEntries', () => {
	const KEY_FIELDS = ['doc_id', 'speech_id', 'chunk_id'];

	it('maps queued un-tags with the same raw-key identity as adds', () => {
		const removes = tagRemovesFromEntries(
			[{ hit: { doc_id: 'd1', speech_id: 0, chunk_id: 19 }, labels: ['speech'] }],
			KEY_FIELDS,
		);
		expect(removes).toEqual([{ doc_id: 'd1', keys: [0, 19], labels: ['speech'] }]);
	});

	it('skips entries whose labels were all cancelled', () => {
		expect(
			tagRemovesFromEntries(
				[{ hit: { doc_id: 'd1', speech_id: 0, chunk_id: 19 }, labels: [] }],
				KEY_FIELDS,
			),
		).toEqual([]);
	});

	it('is arity-generic — single-field identity yields no extra keys', () => {
		expect(
			tagRemovesFromEntries([{ hit: { page_id: 'p7' }, labels: ['figure'] }], ['page_id']),
		).toEqual([{ doc_id: 'p7', keys: [], labels: ['figure'] }]);
	});
});
