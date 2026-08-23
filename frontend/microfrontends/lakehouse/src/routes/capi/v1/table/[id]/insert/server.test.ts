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
			: new Response('{"inserted":3}', {
					status: 200,
					headers: { 'content-type': 'application/json' },
				});
	};
	const headers = opts.contentType ? { 'content-type': opts.contentType } : undefined;
	const request = new Request('http://zone.test/capi/v1/table/t1/insert', {
		method: 'POST',
		body: (opts.bytes ?? new Uint8Array([65, 82, 82, 79, 87])) as BodyInit,
		...(headers ? { headers } : {}),
	});
	const response = await (POST as unknown as (e: unknown) => Promise<Response>)({
		params: { id: 'acme-silver$features' },
		request,
		url: new URL(`http://zone.test/capi/v1/table/t1/insert${opts.search ?? ''}`),
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

describe('the row-insert proxy', () => {
	// The WRITE half of the pair the audit mutation-proved unguarded. It matters more than the read
	// half: this route attaches the caller's bearer to a writer-gated catalog call, so its auth gate is
	// the only thing between an anonymous visitor on an OIDC web tier and a data-plane write.

	it('refuses an anonymous write when auth is on, without the request leaving the BFF', async () => {
		const { response, seen } = await call({ locals: { authEnabled: true, session: null } });

		expect(response.status).toBe(401);
		expect(seen, 'an anonymous write must never reach the catalog').toEqual([]);
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
		// The read side answers `arrow.file`; this side sends `arrow.stream`, because the browser builds
		// an IPC stream here. Swapping them is a silent wire-format mismatch the catalog would reject.
		const { seen } = await call({});
		expect(headersOf(only(seen))['content-type']).toBe('application/vnd.apache.arrow.stream');
	});

	it('keeps a content-type the caller set', async () => {
		const { seen } = await call({ contentType: 'application/vnd.apache.arrow.file' });
		expect(headersOf(only(seen))['content-type']).toBe('application/vnd.apache.arrow.file');
	});

	it('forwards the query string verbatim — ?mode=append is the documented case', async () => {
		const { seen } = await call({ search: '?mode=append' });
		expect(only(seen).url).toBe(
			'http://catalog.test:2333/v1/table/acme-silver%24features/insert?mode=append',
		);
	});

	it('targets the insert route with the table id encoded', async () => {
		const { seen } = await call({});
		expect(only(seen).url).toBe('http://catalog.test:2333/v1/table/acme-silver%24features/insert');
	});

	it('passes an upstream refusal through instead of flattening it', async () => {
		// A writer-gated 403 from the catalog is the answer the UI must show; turning it into a 200 or a
		// generic 500 would make a permission problem look like a bug.
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
