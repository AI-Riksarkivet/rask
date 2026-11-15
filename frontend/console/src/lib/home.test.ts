import { describe, expect, it } from 'vitest';

import { recentCommits, tablesByProject } from './home';

describe('tablesByProject', () => {
	it('counts each project’s tables, projects by name and the estate’s own last', () => {
		const projects: Record<string, string> = { acme: 'acme', beta: 'beta' };
		const rows = tablesByProject(
			[
				'lance_catalog$provenance$events',
				'beta$raw$d',
				'acme$raw$a',
				'acme$raw$b',
				'acme$curated$c',
			],
			(id) => projects[id.split('$')[0] ?? ''] ?? null,
		);
		expect(rows).toEqual([
			{ project: 'acme', total: 3 },
			{ project: 'beta', total: 1 },
			{ project: null, total: 1 },
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
