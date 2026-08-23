import { describe, expect, it, vi } from 'vitest';

vi.mock('$env/dynamic/private', () => ({ env: { CATALOG_API: 'http://catalog.test:2333' } }));

const { POST } = await import('./+server');

type Locals = { authEnabled?: boolean; session?: { accessToken: string } | null };

async function call(opts: {
	search?: string;
	locals?: Locals;
	upstream?: (url: string, init: RequestInit) => Response | Promise<Response>;
}) {
	const seen: { url: string; init: RequestInit }[] = [];
	const fetchImpl = async (url: string, init: RequestInit) => {
		seen.push({ url, init });
		return opts.upstream
			? await opts.upstream(url, init)
			: new Response('{"version":7}', {
					status: 200,
					headers: { 'content-type': 'application/json' },
				});
	};
	const request = new Request('http://zone.test/capi/v1/table/t1/merge_insert', {
		method: 'POST',
		body: new Uint8Array([65, 82, 82, 79, 87]) as BodyInit,
		headers: { 'content-type': 'application/vnd.apache.arrow.stream' },
	});
	const response = await POST({
		params: { id: 't1' },
		request,
		url: new URL(`http://zone.test/capi/v1/table/t1/merge_insert${opts.search ?? '?on=id'}`),
		fetch: fetchImpl as unknown as typeof fetch,
		locals: (opts.locals ?? { authEnabled: true, session: { accessToken: 'tok' } }) as never,
	} as never);
	return { response, seen };
}

// A manual push to bronze uses merge_insert, not a raw insert: `when_not_matched_insert_all` is
// native insert-if-not-matched, so a re-push CONVERGES instead of duplicating. The catalog door
// already accepted it; only the zone-side proxy was missing, so the UI had no way to reach it.
describe('the merge_insert proxy', () => {
	it('targets the catalog door', async () => {
		const { seen } = await call({});
		expect(seen[0]?.url).toBe('http://catalog.test:2333/v1/table/t1/merge_insert?on=id');
	});

	it('forwards the query verbatim, so when_not_matched_insert_all survives', async () => {
		const { seen } = await call({ search: '?on=id&when_not_matched_insert_all=true' });
		expect(seen[0]?.url).toContain('when_not_matched_insert_all=true');
	});

	it('forwards only the signed-in user’s bearer', async () => {
		const { seen } = await call({});
		const headers = seen[0]?.init.headers as Record<string, string>;
		expect(headers.authorization).toBe('Bearer tok');
	});

	// The confused-deputy stance the insert/policy/tag routes already take: an anonymous visitor on
	// an OIDC tier is refused HERE, so the request never leaves the BFF wearing no identity.
	it('refuses an anonymous caller without calling upstream', async () => {
		const { response, seen } = await call({ locals: { authEnabled: true, session: null } });
		expect(response.status).toBe(401);
		expect(seen).toHaveLength(0);
	});

	it('serves an auth-off stack with no bearer', async () => {
		const { seen } = await call({ locals: { authEnabled: false, session: null } });
		const headers = seen[0]?.init.headers as Record<string, string>;
		expect(headers.authorization).toBeUndefined();
		expect(seen).toHaveLength(1);
	});

	it('sends the arrow body through as bytes', async () => {
		const { seen } = await call({});
		const headers = seen[0]?.init.headers as Record<string, string>;
		expect(headers['content-type']).toBe('application/vnd.apache.arrow.stream');
		expect(seen[0]?.init.body).toBeDefined();
	});

	it('passes the upstream status through rather than flattening it', async () => {
		const { response } = await call({
			upstream: () => new Response('{"detail":"writer required"}', { status: 403 }),
		});
		expect(response.status).toBe(403);
	});

	it('answers 502 when the catalog is unreachable', async () => {
		const { response } = await call({
			upstream: () => {
				throw new Error('ECONNREFUSED');
			},
		});
		expect(response.status).toBe(502);
	});
});
