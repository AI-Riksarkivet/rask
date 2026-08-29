/**
 * THE DECLARE → INSERT DEAD END, AND THE DOOR THAT ENDS IT.
 *
 * `declareTable` reserves `<namespace>$<name>` and writes no bytes. Nothing in this zone could then
 * put the first rows in: the append client (`insertRows`) targets `/insert`, which opens the
 * table's dataset to coerce the batch to its schema and therefore 404s for a table that has no
 * dataset yet. The registry offered "Declare table" and the detail page offered "Insert rows", and
 * the path between them did not exist.
 *
 * The op that does exist is `POST /v1/table/{id}/create` with an Arrow-IPC body — the catalog's
 * `_create_table_direct` lands the first data version into a declared-only table's already-reserved
 * location. These gates pin the client for it and the UI command that reaches it, because a door
 * with no caller is the same dead end wearing a different shape.
 */

import { afterEach, describe, expect, it } from 'vitest';
import { globSync, readFileSync } from 'node:fs';

import { createTableWithRows } from './catalog';

/** The zone's real BFF client is exercised, with only the network stubbed — a mocked `$lib/http`
 *  would assert the arguments this module passes rather than the request it actually makes. */
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
	it('posts the Arrow bytes to the catalog create door under this zone base', async () => {
		const { seen } = captureFetch();
		const arrow = new Uint8Array([1, 2, 3]);

		const res = await createTableWithRows('acme-bronze$docs', arrow);

		expect(res.ok).toBe(true);
		// Asserted, not optional-chained: if no request left at all, THAT is the failure to report.
		const [call] = seen;
		if (call === undefined) throw new Error('createTableWithRows made no request');

		expect(call.url).toBe('/lakehouse/capi/v1/table/acme-bronze%24docs/create');
		expect(call.init.method).toBe('POST');
		// The transport ruling: bytes up on a keep-bytes route, JSON ack down.
		expect((call.init.headers as Record<string, string>)['content-type']).toBe(
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

		const res = await createTableWithRows('acme-bronze$docs', new Uint8Array([1]));

		expect(res).toEqual({ ok: false, status: 403, detail: 'no' });
	});
});

describe('the UI can reach it', () => {
	const read = (p: string) => readFileSync(p, 'utf8');

	it('the table registry offers create-with-data beside the bare declare', () => {
		// The registry is the ONE surface reachable for a declared-only table: its detail page has no
		// dataset to describe, so the create-with-data command cannot live there.
		const src = read('src/lib/data/TableRegistry.svelte');

		expect(src, 'the registry must own the create-with-data command').toContain(
			'createTableWithRows',
		);
	});

	it('a bare declare says, at the point of declaring, that the id holds no data yet', () => {
		// The dead end was not only missing a door — it was silent about being a dead end.
		const src = read('src/lib/data/TableRegistry.svelte');

		expect(src).toContain('DECLARE_ONLY_NOTE');
	});

	it('nothing else in the zone re-implements the create door', () => {
		// `src/routes/**` is the BFF proxy that terminates the call — it is the other end of the one
		// client, not a second one. What must not multiply is the browser-side caller.
		const others = globSync('src/**/*.{ts,svelte}')
			.filter((p) => !p.includes('node_modules') && !p.includes('/.svelte-kit/'))
			.filter(
				(p) =>
					p !== 'src/lib/data/catalog.ts' &&
					!p.startsWith('src/routes/') &&
					!p.endsWith('.test.ts'),
			)
			// One segment after the table id, so the BACKED nested creates beside it — `tags/create`,
			// `branches/create`, `version/create` — are not swept up as re-implementations.
			.filter((p) => /v1\/table\/[^`'"/]*\/create\b/.test(read(p)));

		expect(others, 'one client for the create door, like insert and merge_insert').toEqual([]);
	});
});
