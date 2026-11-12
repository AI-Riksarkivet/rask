import { afterEach, describe, expect, it, vi } from 'vitest';

import { catalogListing } from './doors';

/** A catalog that answers each listing request with the next of `pages`, recording the URLs it was asked. */
function catalogAnswering(pages: unknown[]) {
	const asked: string[] = [];
	vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
		asked.push(String(input));
		const body = pages[asked.length - 1];
		return new Response(JSON.stringify(body), { status: body === undefined ? 500 : 200 });
	});
	return asked;
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('catalogListing', () => {
	it.each([
		{
			kind: 'a list',
			key: 'tables',
			token: 'a$t2',
			pages: [{ tables: ['a$t1', 'a$t2'], page_token: 'a$t2' }, { tables: ['a$t3'] }],
			rows: ['a$t1', 'a$t2', 'a$t3'],
		},
		{
			kind: 'a map',
			key: 'tags',
			token: 'v1',
			pages: [{ tags: { v1: { version: 1 } }, page_token: 'v1' }, { tags: { v2: { version: 2 } } }],
			rows: { v1: { version: 1 }, v2: { version: 2 } },
		},
	])(
		'reads every page of $kind, following each page_token',
		async ({ key, token, pages, rows }) => {
			const asked = catalogAnswering(pages);

			const answer = await catalogListing('/v1/namespace/a/table/list', key);

			expect(answer).toEqual({ ok: true, data: { [key]: rows } });
			expect(asked).toEqual([
				'/api/catalog/v1/namespace/a/table/list',
				`/api/catalog/v1/namespace/a/table/list?page_token=${encodeURIComponent(token)}`,
			]);
		},
	);
});
