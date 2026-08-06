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

/** The areas that can actually be OPENED — every area is always listed now, so "offered" means
 *  "not carrying an `unavailable` reason". */
function openable(nav: ReturnType<typeof explorerZoneNav>): string[] {
	return (nav.groups.find((g) => g.label === 'Explore')?.items ?? [])
		.filter((i) => !i.unavailable)
		.map((i) => i.title);
}

/** The reason a named area gives for being closed, or undefined when it is open. */
function why(nav: ReturnType<typeof explorerZoneNav>, title: string): string | undefined {
	return (nav.groups.find((g) => g.label === 'Explore')?.items ?? []).find((i) => i.title === title)
		?.unavailable;
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
	it('LISTS every area always — an absent row reads as a missing product feature', () => {
		// The ruling reversed here. Hiding was fail-closed and defensible, and it made a corpus with
		// no knowledge graph look like a product with no knowledge graph — two different claims, and
		// only the second is false. Every area is present; the ones this corpus cannot do are closed
		// and say why.
		expect(explore(explorerZoneNav(view()))).toEqual(['Search', 'Atlas', 'Tree', 'Graph']);
	});

	it('opens ONLY Search on a corpus that declares nothing', () => {
		expect(openable(explorerZoneNav(view()))).toEqual(['Search']);
	});

	it('opens Atlas only when an atlas space is declared', () => {
		expect(openable(explorerZoneNav(view()))).not.toContain('Atlas');
		expect(openable(explorerZoneNav(view({ atlas: [ATLAS_SPACE] })))).toContain('Atlas');
	});

	it('opens Tree only when a topic hierarchy is declared', () => {
		// The capability name the SERVICE reads (`topics.py` looks it up to find the hierarchy
		// table), so a declaration cannot mean one thing to the sidebar and another to the endpoint.
		expect(openable(explorerZoneNav(view()))).not.toContain('Tree');
		expect(openable(explorerZoneNav(view({ capabilities: { topics: 'topics.tree' } })))).toContain(
			'Tree',
		);
	});

	it('opens Graph only when a knowledge graph is declared', () => {
		// `/graph` consulted the descriptor NOWHERE — zero references — so it was offered on every
		// corpus in the estate whether or not one had ever been built.
		expect(openable(explorerZoneNav(view()))).not.toContain('Graph');
		expect(openable(explorerZoneNav(view({ capabilities: { graph: 'kg' } })))).toContain('Graph');
	});

	it('a closed area says why, about THE CORPUS rather than the product', () => {
		// "unavailable" tells you nothing to do. Naming the missing declaration does: build it,
		// declare it, and the door opens.
		const nav = explorerZoneNav(view());

		expect(why(nav, 'Graph')).toMatch(/corpus/i);
		expect(why(nav, 'Graph')).toMatch(/graph/i);
		expect(why(nav, 'Tree')).toMatch(/topic/i);
		expect(why(nav, 'Atlas')).toMatch(/atlas/i);
	});

	it('an OPEN area carries no reason', () => {
		expect(why(explorerZoneNav(view({ capabilities: { graph: 'kg' } })), 'Graph')).toBeUndefined();
	});

	it('a capability for something ELSE opens no door', () => {
		// The exact shape of the reported bug: `transcripts_v2` declares `frames` and got all four
		// OPEN. It still lists all four — it just cannot open three of them.
		const nav = explorerZoneNav(view({ capabilities: { frames: 'chunks.image' } }));

		expect(openable(nav)).toEqual(['Search']);
	});

	it('opens everything a fully-declared corpus supports', () => {
		const nav = explorerZoneNav(
			view({ atlas: [ATLAS_SPACE], capabilities: { topics: 'topics.tree', graph: 'kg' } }),
		);

		expect(openable(nav)).toEqual(['Search', 'Atlas', 'Tree', 'Graph']);
	});

	it('keeps the declared ORDER rather than the order things were added', () => {
		const nav = explorerZoneNav(view({ atlas: [ATLAS_SPACE], capabilities: { graph: 'kg' } }));

		expect(openable(nav)).toEqual(['Search', 'Atlas', 'Graph']);
		// …and the closed one still holds its place in the list rather than being appended.
		expect(explore(nav)).toEqual(['Search', 'Atlas', 'Tree', 'Graph']);
	});
});

describe('before a descriptor exists', () => {
	it('shows the rail its FULL shape, with everything conditional closed', () => {
		// The rail no longer grows rows as data arrives. It used to render only Search until the
		// descriptor landed, which is what made the estate look broken every time the viewer was
		// down — the areas appeared to have been removed rather than to be waiting.
		expect(explore(explorerZoneNav(null))).toEqual(['Search', 'Atlas', 'Tree', 'Graph']);
		expect(openable(explorerZoneNav(null))).toEqual(['Search']);
	});

	it('says it is LOADING, not that the corpus lacks the feature', () => {
		// Two different sentences, and telling a user their corpus has no knowledge graph while the
		// descriptor is still in flight is simply false.
		expect(why(explorerZoneNav(null), 'Graph')).toMatch(/loading/i);
	});
});

describe('the rest of the rail', () => {
	it('drops the Help group entirely — a Guide is not an area of the corpus', () => {
		const nav = explorerZoneNav(view());

		expect(nav.groups.map((g) => g.label)).not.toContain('Help');
		expect(nav.groups.flatMap((g) => g.items.map((i) => i.title))).not.toContain('Guide');
	});

	it('keeps Workflow and Workbench regardless of the corpus, in ONE pinned group', () => {
		// Neither is a VIEW of the data, so neither depends on what the data declares — and by that
		// same argument neither is an AREA, so both are pinned rather than scrolling among the areas.
		const bare = explorerZoneNav(view());

		expect(bare.footer?.items.map((i) => i.title)).toEqual(['Workflow', 'Workbench']);
	});

	it('never renders the same group LABEL twice', () => {
		// The defect this exists for. "Workspace" was declared twice — once as a scrolling group
		// holding Workflow, once as the pinned footer holding Workbench — so the rail drew the same
		// heading in two places with a single row under each, which reads as a rendering bug rather
		// than a deliberate split. Nothing asserted label uniqueness, which is how it shipped.
		const nav = explorerZoneNav(view());
		const labels = [...nav.groups.map((g) => g.label), ...(nav.footer ? [nav.footer.label] : [])];

		expect(labels).toEqual([...new Set(labels)]);
	});
});

describe('the Atlas gate follows the ACTIVE TABLE, not just the corpus', () => {
	const spaceOn = (table: string) => ({ ...ATLAS_SPACE, table });

	it('opens Atlas when a space is bound to the table being searched', () => {
		const nav = explorerZoneNav(view({ atlas: [spaceOn('pages')] }), 'pages');

		expect(openable(nav)).toContain('Atlas');
	});

	it('CLOSES Atlas when every space belongs to a different table', () => {
		// The reason this gate is table-sensitive at all: an atlas over `pages` has nothing to draw
		// while you are searching `lines`, so offering it leads to a page that can only say so.
		const nav = explorerZoneNav(view({ atlas: [spaceOn('pages')] }), 'lines');

		expect(openable(nav)).not.toContain('Atlas');
		// …and the reason names the TABLE, not the corpus — the corpus does have an atlas.
		expect(why(nav, 'Atlas')).toMatch(/table/i);
	});

	it('falls back to ANY space when no table is active', () => {
		// The single-table corpus every descriptor on disk still describes.
		const nav = explorerZoneNav(view({ atlas: [spaceOn('pages')] }), null);

		expect(openable(nav)).toContain('Atlas');
	});

	it('leaves Tree and Graph alone — capabilities are declared per CORPUS', () => {
		// The asymmetry is a property of the descriptor, not an oversight: `capabilities` is declared
		// once per corpus, so switching tables cannot change it.
		const declared = view({ capabilities: { topics: 'topics.tree', graph: 'kg' } });

		expect(openable(explorerZoneNav(declared, 'pages'))).toEqual(['Search', 'Tree', 'Graph']);
		expect(openable(explorerZoneNav(declared, 'lines'))).toEqual(['Search', 'Tree', 'Graph']);
	});
});
