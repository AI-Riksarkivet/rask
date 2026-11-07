/**
 * THE DECLARE → INSERT DEAD END, AND THE DOOR THAT ENDS IT.
 *
 * `declareTable` reserves `<namespace>$<name>` and writes no bytes. Nothing could then
 * put the first rows in: the append client (`insertRows`) targets `/insert`, which opens the
 * table's dataset to coerce the batch to its schema and therefore 404s for a table that has no
 * dataset yet. The registry offered "Declare table" and the detail page offered "Insert rows", and
 * the path between them did not exist.
 *
 * The op that does exist is `POST /v1/table/{id}/create` with an Arrow-IPC body — the catalog's
 * `_create_table_direct` lands the first data version into a declared-only table's already-reserved
 * location. These pin the client for it.
 */

import { afterEach, describe, expect, it } from 'vitest';

import { createTableWithRows } from './catalog';

/** The real client is exercised, with only the network stubbed — a mocked `#lib/http` would assert
 *  the arguments this module passes rather than the request it actually makes. */
const realFetch = globalThis.fetch;
afterEach(() => {
	globalThis.fetch = realFetch;
});

function captureFetch(): { seen: { url: string; init: RequestInit }[] } {
	const seen: { url: string; init: RequestInit }[] = [];
	globalThis.fetch = ((url: string, init: RequestInit) => {
		seen.push({ url, init });
		return Promise.resolve(
			new Response('{"version":1}', {
				status: 200,
				headers: { 'content-type': 'application/json' },
			}),
		);
	}) as unknown as typeof fetch;
	return { seen };
}

describe('createTableWithRows', () => {
	it('posts the Arrow bytes to the catalog create door', async () => {
		const { seen } = captureFetch();
		const arrow = new Uint8Array([1, 2, 3]);

		const res = await createTableWithRows({ table: 'acme-bronze$docs', arrow });

		expect(res.ok).toBe(true);
		// Asserted, not optional-chained: if no request left at all, THAT is the failure to report.
		const [call] = seen;
		if (call === undefined) throw new Error('createTableWithRows made no request');

		expect(call.url).toBe('/api/catalog/v1/table/acme-bronze%24docs/create');
		expect(call.init.method).toBe('POST');
		// Bytes up, JSON ack down.
		expect(new Headers(call.init.headers).get('content-type')).toBe(
			'application/vnd.apache.arrow.stream',
		);
		expect(Array.from(call.init.body as Uint8Array)).toEqual([1, 2, 3]);
	});

	it('surfaces an upstream refusal instead of flattening it', async () => {
		globalThis.fetch = (() =>
			Promise.resolve(
				new Response('{"detail":"no"}', {
					status: 403,
					headers: { 'content-type': 'application/json' },
				}),
			)) as unknown as typeof fetch;

		const res = await createTableWithRows({
			table: 'acme-bronze$docs',
			arrow: new Uint8Array([1]),
		});

		expect(res).toEqual({ ok: false, status: 403, detail: 'no' });
	});
});
