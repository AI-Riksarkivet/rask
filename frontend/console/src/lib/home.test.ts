import { describe, expect, it } from 'vitest';

import { recentCommits, tablesByProjectAndTier } from './home';

describe('tablesByProjectAndTier', () => {
	it('counts each project’s tables per declared tier, an untiered table apart, the estate’s own last', () => {
		const projects: Record<string, string> = { acme: 'acme', beta: 'beta' };
		// Declared on the namespace, whatever it is called: `acme$gold` declares nothing here.
		const tiers: Record<string, 'bronze' | 'silver'> = {
			acme$bronze: 'bronze',
			beta$locked: 'silver',
		};
		const rows = tablesByProjectAndTier(
			[
				'acme$bronze$a',
				'acme$bronze$b',
				'acme$gold$c',
				'beta$locked$d',
				'lance_catalog$provenance$events',
			],
			(id) => projects[id.split('$')[0] ?? ''] ?? null,
			(id) => {
				const tier = tiers[id.split('$').slice(0, -1).join('$')];
				return tier ? { stage: tier } : null;
			},
		);
		expect(rows).toEqual([
			{ project: 'acme', bronze: 2, silver: 0, gold: 0, other: 1, total: 3 },
			{ project: 'beta', bronze: 0, silver: 1, gold: 0, other: 0, total: 1 },
			{ project: null, bronze: 0, silver: 0, gold: 0, other: 1, total: 1 },
		]);
	});
});

describe('recentCommits', () => {
	it('names what each commit did, from its job, and which tables it wrote', () => {
		expect(
			recentCommits([
				{
					seq: 9,
					event_type: 'COMPLETE',
					event_time: '2026-11-15T19:11:53Z',
					job: 'lance-catalog/insert.acme$bronze$orders',
					author: 'dex~alice',
					outputs: ['acme$bronze$orders'],
				},
				{ seq: 8, event_type: 'START', job: null, outputs: [] },
			]),
		).toEqual([
			{
				seq: 9,
				at: '2026-11-15T19:11:53Z',
				operation: 'insert',
				tables: ['acme$bronze$orders'],
				author: 'dex~alice',
			},
			{ seq: 8, at: null, operation: 'START', tables: [], author: null },
		]);
	});
});
