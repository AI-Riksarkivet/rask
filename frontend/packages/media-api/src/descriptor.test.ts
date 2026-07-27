import * as v from 'valibot';
import { describe, expect, it } from 'vitest';
import { DatasetDescriptorSchema, DatasetView, type Row } from './descriptor';

// A transcripts-shaped descriptor (3-part identity, time axis, media, all modes).
const TRANSCRIPTS = {
	id: 'transcripts_v2',
	tables: {
		chunks: {
			name: 'chunks',
			row_count: 100,
			version: 3,
			columns: [
				{ name: 'doc_id', arrow_type: 'string', nullable: false },
				{
					name: 'semantic_vec',
					arrow_type: 'fixed_size_list<float>[2048]',
					nullable: true,
					vector_dim: 2048,
				},
			],
			indexes: [],
		},
	},
	declared: {
		identity: {
			key_fields: ['doc_id', 'seg_id', 'chunk_id'],
			doc_key: 'doc_id',
			doc_key_pattern: '^[a-f0-9]{16}$',
		},
		document: {
			table: 'documents',
			media_blob: 'media_blob',
			mime: 'media_mime',
			thumbnail: 'thumbnail',
		},
		time: { start: 'start', end: 'end' },
		display: {
			title: ['title_name', 'audio_path', 'doc_id'],
			body: 'text',
			caption: 'caption',
			metadata: [
				{ field: 'title_name', label: 'Name' },
				{ field: 'ref_code', label: 'Ref' },
			],
		},
		search: {
			row_table: 'chunks',
			fts: { table: 'chunks', column: 'text', language: 'Swedish' },
			vectors: {
				semantic: { table: 'chunks', column: 'semantic_vec', dim: 2048, query_encoder: 'text' },
				visual: {
					table: 'chunk_frames',
					column: 'frame_embedding',
					dim: 2048,
					query_encoder: 'image',
				},
				scene: {
					table: 'chunk_frames',
					column: 'caption_embedding',
					dim: 2048,
					query_encoder: 'text',
					caption_source: 'caption',
				},
			},
			filterable: ['language', 'title_name', 'ref_code'],
			rerank: true,
		},
		atlas: [
			{
				name: 'text',
				x: 'atlas_x',
				y: 'atlas_y',
				cluster: 'atlas_cluster',
				source_column: 'semantic_vec',
				table: 'chunks',
				channels: [
					{ name: 'language', column: 'language' },
					{ name: 'topic', broadest_prefix: 'topic_l' },
				],
			},
		],
		capabilities: { diarization: 'speaker_turns', voice: 'speaker_embeddings.embedding' },
	},
};

// A structurally different corpus: single-key identity, NO time axis, no vectors.
const IMAGES = {
	id: 'images',
	tables: {
		items: {
			name: 'items',
			row_count: 5,
			version: 1,
			columns: [{ name: 'item_id', arrow_type: 'string', nullable: false }],
			indexes: [],
		},
	},
	declared: {
		identity: { key_fields: ['item_id'], doc_key: 'item_id', doc_key_pattern: '.*' },
		document: { table: 'items', media_blob: 'blob', mime: 'mime' },
		time: null,
		display: {
			title: ['label'],
			body: 'caption',
			caption: null,
			metadata: [{ field: 'label', label: 'Label' }],
		},
		search: {
			row_table: 'items',
			fts: { table: 'items', column: 'caption', language: 'English' },
			vectors: {},
			filterable: ['label'],
			rerank: false,
		},
		atlas: [],
		capabilities: {},
	},
};

function view(raw: unknown): DatasetView {
	return new DatasetView(v.parse(DatasetDescriptorSchema, raw));
}

describe('DatasetView — transcripts corpus', () => {
	const v = view(TRANSCRIPTS);

	it('composes the row key from the descriptor identity, not a hardcoded shape', () => {
		const row = { doc_id: 'abc', seg_id: 2, chunk_id: 7 } as Row;
		expect(v.rowKey(row)).toBe('abc|2|7');
		expect(v.keyFields).toEqual(['doc_id', 'seg_id', 'chunk_id']);
	});

	it('picks the first non-empty declared title field', () => {
		expect(v.title({ title_name: 'Press conf', audio_path: 'x.mp4', doc_id: 'abc' } as Row)).toBe(
			'Press conf',
		);
		expect(v.title({ title_name: null, audio_path: 'x.mp4', doc_id: 'abc' } as Row)).toBe('x.mp4');
		expect(v.title({ doc_id: 'abc' } as Row)).toBe('abc');
	});

	it('resolves body, caption, and declared metadata rows', () => {
		const row = { text: 'hello', caption: 'a frame', title_name: 'N', ref_code: 'R1' } as Row;
		expect(v.body(row)).toBe('hello');
		expect(v.caption(row)).toBe('a frame');
		expect(v.metadata(row)).toEqual([
			{ field: 'title_name', label: 'Name', value: 'N' },
			{ field: 'ref_code', label: 'Ref', value: 'R1' },
		]);
	});

	it('derives search modes from declared bindings', () => {
		expect(v.searchModes).toEqual([
			'fts',
			'semantic',
			'visual',
			'scene',
			'scene_fts',
			'hybrid',
			'all',
		]);
		expect(v.hasMode('hybrid')).toBe(true);
	});

	it('builds identity-arity media URLs', () => {
		const row = { doc_id: 'abc', seg_id: 2, chunk_id: 7 } as Row;
		expect(v.mediaUrl(row)).toBe('/api/media/abc');
		expect(v.frameUrl(row)).toBe('/api/chunk-frame/abc/2/7');
	});

	it('normalizes relevance so higher is always better', () => {
		expect(v.relevanceOf({ _distance: 0.4 } as Row, 'semantic')).toBeCloseTo(1.6);
		expect(v.relevanceOf({ _score: 3 } as Row, 'fts')).toBe(3);
	});

	it('exposes atlas channels including broadest-prefix', () => {
		expect(v.atlasChannels('text')).toEqual(['language', 'topic']);
	});

	it('reports capabilities from the descriptor', () => {
		expect(v.hasCapability('diarization')).toBe(true);
		expect(v.hasCapability('graph')).toBe(false);
	});
});

describe('DatasetView — structurally different corpus (acid-test shape)', () => {
	const v = view(IMAGES);

	it('handles a single-field identity', () => {
		expect(v.rowKey({ item_id: 'i9' } as Row)).toBe('i9');
		expect(v.mediaUrl({ item_id: 'i9' } as Row)).toBe('/api/media/i9');
		expect(v.frameUrl({ item_id: 'i9' } as Row)).toBe('/api/chunk-frame/i9');
	});

	it('reports no time axis and no vector modes (fts only)', () => {
		expect(v.hasTime).toBe(false);
		expect(v.time({ item_id: 'i9' } as Row)).toBeNull();
		expect(v.searchModes).toEqual(['fts']);
	});

	it('carries a dataset param for a non-default dataset', () => {
		const nonDefault = view(IMAGES).withDatasetParam(false);
		expect(nonDefault.mediaUrl({ item_id: 'i9' } as Row)).toBe('/api/media/i9?dataset=images');
		expect(nonDefault.datasetParam()).toBe('images');
	});
});

describe('type-category model — schema-agnostic by Lance type', () => {
	const v = view(TRANSCRIPTS);

	it('classifies columns by Lance type, not by role', () => {
		expect(v.columnCategory('chunks', 'semantic_vec')).toBe('embedding'); // fixed_size_list<float>
		expect(v.columnCategory('chunks', 'doc_id')).toBe('categorical'); // string
		expect(v.columnCategory('chunks', 'nope')).toBeNull();
	});

	it('treats every embedding as ONE uniform category (text/pixel/scene alike)', () => {
		const spaces = v.vectorSpaces;
		expect(spaces.map((s) => s.key)).toEqual(['semantic', 'visual', 'scene']);
		// the ONLY per-space distinction is on-row-table vs frame-join — from the binding
		expect(spaces.find((s) => s.key === 'semantic')?.onRowTable).toBe(true);
		expect(spaces.find((s) => s.key === 'visual')?.onRowTable).toBe(false);
		expect(spaces.find((s) => s.key === 'scene')?.captionSource).toBe('caption');
	});

	it('derives search modes generically (identical to the old fixed set)', () => {
		expect(v.searchModes).toEqual([
			'fts',
			'semantic',
			'visual',
			'scene',
			'scene_fts',
			'hybrid',
			'all',
		]);
	});

	it('a NEW text-encoder embedding key flows through as a mode with ZERO code edits', () => {
		const withDense = structuredClone(TRANSCRIPTS);
		withDense.tables.chunks.columns.push({
			name: 'dense_vec',
			arrow_type: 'fixed_size_list<float>[1024]',
			nullable: true,
			vector_dim: 1024,
		});
		(withDense.declared.search.vectors as Record<string, unknown>).dense = {
			table: 'chunks',
			column: 'dense_vec',
			dim: 1024,
			query_encoder: 'text',
		};
		const dv = view(withDense);
		expect(dv.columnCategory('chunks', 'dense_vec')).toBe('embedding');
		expect(dv.vectorSpaces.map((s) => s.key)).toContain('dense');
		expect(dv.searchModes).toContain('dense'); // a text-queryable space surfaces — the point
	});

	it('a foreign-encoder embedding is enumerated but NOT offered as a search mode', () => {
		// A voiceprint space (query_encoder the search box can't produce) is a real
		// declared space — it shows up in vectorSpaces — but advertising it as a
		// text/image search mode would 400 in the backend, so searchModes omits it.
		const withAudio = structuredClone(TRANSCRIPTS);
		withAudio.tables.chunks.columns.push({
			name: 'voice_vec',
			arrow_type: 'fixed_size_list<float>[256]',
			nullable: true,
			vector_dim: 256,
		});
		(withAudio.declared.search.vectors as Record<string, unknown>).audio = {
			table: 'chunks',
			column: 'voice_vec',
			dim: 256,
			query_encoder: 'audio',
		};
		const av = view(withAudio);
		expect(av.vectorSpaces.map((s) => s.key)).toContain('audio'); // enumerated
		expect(av.searchModes).not.toContain('audio'); // but not a search-box mode
	});
});
