import { afterEach, describe, expect, it, vi } from 'vitest';

import { everyTuple } from './access.api';

/** A catalog that answers each tuple-page request with the next of `pages`, recording the URLs it was asked. */
function catalogAnswering(pages: unknown[]) {
	const asked: string[] = [];
	vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
		asked.push(String(input));
		const body = pages[asked.length - 1];
		return new Response(JSON.stringify(body), { status: body === undefined ? 500 : 200 });
	});
	return asked;
}

const tuple = (user: string) => ({ user, relation: 'reader', object: 'table:t1', condition: null });

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('everyTuple', () => {
	it('reads every page of one object, following each continuation token (LH-577 W4)', async () => {
		const asked = catalogAnswering([
			{ tuples: [tuple('user:a'), tuple('user:b')], continuation: 'c1' },
			{ tuples: [tuple('user:c')], continuation: null },
		]);

		const answer = await everyTuple({ object: 'table:t1' });

		expect(answer).toEqual({
			ok: true,
			data: { tuples: [tuple('user:a'), tuple('user:b'), tuple('user:c')], truncated: false },
		});
		expect(asked).toEqual([
			'/api/catalog/management/v1/access/tuples?page_size=100&object=table%3At1',
			'/api/catalog/management/v1/access/tuples?page_size=100&object=table%3At1&continuation=c1',
		]);
	});
});
