import { describe, expect, it, vi } from 'vitest';

vi.mock('$env/dynamic/private', () => ({ env: { CATALOG_API: 'http://catalog.test:2333' } }));

const { POST } = await import('./+server');

type Locals = { authEnabled?: boolean; session?: { accessToken: string } | null };

async function call(opts: {
	bytes?: Uint8Array;
	contentType?: string;
	search?: string;
	locals?: Locals;
	upstream?: (url: string, init: RequestInit) => Response | Promise<Response>;
}) {
	const seen: { url: string; init: RequestInit }[] = [];
	const fetchImpl = async (url: string, init: RequestInit) => {
		seen.push({ url, init });
		return opts.upstream
			? await opts.upstream(url, init)
			: new Response('{"version":1}', {
					status: 200,
					headers: { 'content-type': 'application/json' },
				});
	};
	const headers = opts.contentType ? { 'content-type': opts.contentType } : undefined;
	const request = new Request('http://zone.test/capi/v1/table/t1/create', {
		method: 'POST',
		body: (opts.bytes ?? new Uint8Array([65, 82, 82, 79, 87])) as BodyInit,
		...(headers ? { headers } : {}),
	});
	const response = await (POST as unknown as (e: unknown) => Promise<Response>)({
		params: { id: 'acme-bronze$docs' },
		request,
		url: new URL(`http://zone.test/capi/v1/table/t1/create${opts.search ?? ''}`),
		fetch: fetchImpl,
		locals: opts.locals ?? {},
	});
	return { response, seen };
}

/** The single upstream call, asserted rather than optional-chained — if the handler never called out,
 *  that is the failure to report, not a `TypeError` three lines later. */
function only(seen: { url: string; init: RequestInit }[]) {
	if (seen.length !== 1) throw new Error(`expected exactly one upstream call, got ${seen.length}`);
	return seen[0] as { url: string; init: RequestInit };
}

const headersOf = (call: { init: RequestInit }) => call.init.headers as Record<string, string>;

/**
 * THE FIRST WRITE OF A TABLE HAS ITS OWN DOOR, AND THE ZONE WAS MISSING IT.
 *
 * `POST /declare` reserves an id and writes no bytes, so the reserved table has no dataset at its
 * location. The append door (`/insert`) opens that dataset to coerce the incoming batch to its
 * schema, so it answers 404 for exactly the table a user just declared — declare → insert was a
 * dead end with no way out of it in the UI.
 *
 * The catalog already handles this: `dataplane._create_table_direct` documents that a declared-only
 * table "has no readable dataset, so every mode simply lands the first data version into its
 * already-declared location". Only the zone-side proxy was absent, so the browser could not reach
 * the one op that ends the dead end.
 *
 * Mirrors the `insert` and `merge_insert` proxies beside it exactly — session-only, binary body
 * streamed through, query forwarded verbatim so `?mode=` survives — because three proxies to one
 * catalog that differ in their auth stance is how one of them ends up wrong.
 */
describe('the create-with-data proxy', () => {
	it('refuses an anonymous write when auth is on, without the request leaving the BFF', async () => {
		const { response, seen } = await call({ locals: { authEnabled: true, session: null } });

		expect(response.status).toBe(401);
		expect(seen, 'an anonymous create must never reach the catalog').toEqual([]);
	});

	it('forwards the signed-in bearer', async () => {
		const { seen } = await call({
			locals: { authEnabled: true, session: { accessToken: 'tok-xyz' } },
		});
		expect(headersOf(only(seen))['authorization']).toBe('Bearer tok-xyz');
	});

	it('streams the Arrow bytes through unchanged', async () => {
		const bytes = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]);
		const { seen } = await call({ bytes });

		const sent = new Uint8Array(only(seen).init.body as ArrayBuffer);
		expect(Array.from(sent)).toEqual(Array.from(bytes));
	});

	it('defaults the content-type to the Arrow STREAM form, not the file form', async () => {
		// The browser builds an IPC stream here, exactly as it does for insert. Swapping the two is a
		// silent wire-format mismatch the catalog would reject.
		const { seen } = await call({});
		expect(headersOf(only(seen))['content-type']).toBe('application/vnd.apache.arrow.stream');
	});

	it('forwards the query string verbatim — ?mode= is the documented case', async () => {
		const { seen } = await call({ search: '?mode=ExistOk' });
		expect(only(seen).url).toBe(
			'http://catalog.test:2333/v1/table/acme-bronze%24docs/create?mode=ExistOk',
		);
	});

	it('targets the create route with the table id encoded', async () => {
		const { seen } = await call({});
		expect(only(seen).url).toBe('http://catalog.test:2333/v1/table/acme-bronze%24docs/create');
	});

	it('passes an upstream refusal through instead of flattening it', async () => {
		// A create-gated 403 from the catalog is the answer the UI must show; turning it into a 200 or
		// a generic 500 would make a permission problem look like a bug.
		const { response } = await call({
			upstream: () => new Response('{"detail":"no"}', { status: 403 }),
		});
		expect(response.status).toBe(403);
	});

	it('answers 502 when the upstream cannot be reached', async () => {
		const { response } = await call({
			upstream: () => {
				throw new Error('ECONNREFUSED');
			},
		});
		expect(response.status).toBe(502);
	});
});
