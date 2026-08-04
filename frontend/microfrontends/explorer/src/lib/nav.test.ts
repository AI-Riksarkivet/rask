/**
 * The zone sidebar is DERIVED from the active dataset, not a static list.
 *
 * It used to be a const listing Search/Atlas/Tree/Graph unconditionally. The seeded `transcripts_v2`
 * declares `atlas: []` and one capability (`frames`) — and all four rendered. Clicking Atlas on a
 * corpus with no embedding spaces reached a page whose only possible answer was that the thing it
 * exists to show is not here, which reads as a broken product rather than an inapplicable view.
 *
 * The gates are asserted against REAL descriptors parsed by the real schema, because the bug was
 * precisely that the nav never consulted one.
 */

import { DatasetDescriptorSchema, DatasetView } from '@rask/explorer-api/descriptor';
import * as v from 'valibot';
import { describe, expect, it } from 'vitest';

import { explorerZoneNav } from './nav';

/** The minimum a descriptor must carry — everything conditional is added per test. */
function descriptor(extra: Record<string, unknown> = {}): unknown {
	return {
		id: 'corpus',
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
			document: { table: 'items', media_blob: 'blob', mime: 'mime' },
			time: null,
			display: { title: ['label'], body: 'caption', caption: null, metadata: [] },
			search: {
				row_table: 'items',
				fts: { table: 'items', column: 'caption', language: 'English' },
				vectors: {},
				filterable: [],
				rerank: false,
			},
			atlas: [],
			capabilities: {},
			...extra,
		},
	};
}

const view = (extra: Record<string, unknown> = {}): DatasetView =>
	new DatasetView(v.parse(DatasetDescriptorSchema, descriptor(extra)));

/** The titles in the "Explore" group — the set under test. */
function explore(nav: ReturnType<typeof explorerZoneNav>): string[] {
	return (nav.groups.find((g) => g.label === 'Explore')?.items ?? []).map((i) => i.title);
}

const ATLAS_SPACE = {
	name: 'text',
	x: 'atlas_x',
	y: 'atlas_y',
	cluster: 'atlas_cluster',
	source_column: 'semantic_vec',
	table: 'items',
	channels: [],
};

describe('the Explore areas follow what the corpus declares', () => {
	it('offers ONLY Search on a corpus that declares nothing', () => {
		expect(explore(explorerZoneNav(view()))).toEqual(['Search']);
	});

	it('offers Atlas only when embedding spaces are declared', () => {
		expect(explore(explorerZoneNav(view()))).not.toContain('Atlas');
		expect(explore(explorerZoneNav(view({ atlas: [ATLAS_SPACE] })))).toContain('Atlas');
	});

	it('offers Tree only when a topic hierarchy is declared', () => {
		// The capability name the SERVICE reads (`topics.py` looks it up to find the hierarchy
		// table), so a declaration cannot mean one thing to the sidebar and another to the endpoint.
		expect(explore(explorerZoneNav(view()))).not.toContain('Tree');
		expect(explore(explorerZoneNav(view({ capabilities: { topics: 'topics.tree' } })))).toContain(
			'Tree',
		);
	});

	it('offers Graph only when a knowledge graph is declared', () => {
		// `/graph` consulted the descriptor NOWHERE — zero references — so it was offered on every
		// corpus in the estate whether or not one had ever been built.
		expect(explore(explorerZoneNav(view()))).not.toContain('Graph');
		expect(explore(explorerZoneNav(view({ capabilities: { graph: 'kg' } })))).toContain('Graph');
	});

	it('a capability for something ELSE does not open a door', () => {
		// The exact shape of the reported bug: `transcripts_v2` declares `frames` and got all four.
		const nav = explorerZoneNav(view({ capabilities: { frames: 'chunks.image' } }));

		expect(explore(nav)).toEqual(['Search']);
	});

	it('offers everything a fully-declared corpus supports', () => {
		const nav = explorerZoneNav(
			view({ atlas: [ATLAS_SPACE], capabilities: { topics: 'topics.tree', graph: 'kg' } }),
		);

		expect(explore(nav)).toEqual(['Search', 'Atlas', 'Tree', 'Graph']);
	});

	it('keeps the declared ORDER rather than the order things were added', () => {
		const nav = explorerZoneNav(view({ atlas: [ATLAS_SPACE], capabilities: { graph: 'kg' } }));

		expect(explore(nav)).toEqual(['Search', 'Atlas', 'Graph']);
	});
});

describe('before a descriptor exists', () => {
	it('offers only what is certain rather than everything', () => {
		// Fail-closed, the estate's posture everywhere else (Settings is ABSENT for a non-admin, not
		// disabled). The layout already withholds the page until the descriptor resolves, so a rail
		// showing only Search matches the page beside it.
		expect(explore(explorerZoneNav(null))).toEqual(['Search']);
	});
});

describe('the rest of the rail', () => {
	it('drops the Help group entirely — a Guide is not an area of the corpus', () => {
		const nav = explorerZoneNav(view());

		expect(nav.groups.map((g) => g.label)).not.toContain('Help');
		expect(nav.groups.flatMap((g) => g.items.map((i) => i.title))).not.toContain('Guide');
	});

	it('keeps Workflow and the pinned Workbench regardless of the corpus', () => {
		// Neither is a VIEW of the data, so neither depends on what the data declares.
		const bare = explorerZoneNav(view());

		expect(bare.groups.flatMap((g) => g.items.map((i) => i.title))).toContain('Workflow');
		expect(bare.footer?.items.map((i) => i.title)).toEqual(['Workbench']);
	});
});

describe('the Atlas gate follows the ACTIVE TABLE, not just the corpus', () => {
	const spaceOn = (table: string) => ({ ...ATLAS_SPACE, table });

	it('offers Atlas when a space is bound to the table being searched', () => {
		const nav = explorerZoneNav(view({ atlas: [spaceOn('pages')] }), 'pages');

		expect(explore(nav)).toContain('Atlas');
	});

	it('HIDES Atlas when every space belongs to a different table', () => {
		// The reason this gate is table-sensitive at all: an atlas over `pages` has nothing to draw
		// while you are searching `lines`, so offering it leads to a page that can only say so.
		const nav = explorerZoneNav(view({ atlas: [spaceOn('pages')] }), 'lines');

		expect(explore(nav)).not.toContain('Atlas');
	});

	it('falls back to ANY space when no table is active', () => {
		// The single-table corpus every descriptor on disk still describes.
		const nav = explorerZoneNav(view({ atlas: [spaceOn('pages')] }), null);

		expect(explore(nav)).toContain('Atlas');
	});

	it('leaves Tree and Graph alone — capabilities are declared per CORPUS', () => {
		// The asymmetry is a property of the descriptor, not an oversight: `capabilities` is declared
		// once per corpus, so switching tables cannot change it.
		const declared = view({ capabilities: { topics: 'topics.tree', graph: 'kg' } });

		expect(explore(explorerZoneNav(declared, 'pages'))).toEqual(['Search', 'Tree', 'Graph']);
		expect(explore(explorerZoneNav(declared, 'lines'))).toEqual(['Search', 'Tree', 'Graph']);
	});
});
