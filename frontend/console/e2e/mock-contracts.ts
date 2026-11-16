// The catalog's contract read (ADR 0212) for the two mock catalogs (`mock-catalog.ts` and
// `admin/mock-catalog.ts`): an unseeded `contract/describe` answers as the catalog does for a table that
// holds no contract, `contract: null`. A spec that means a contract, or a refusal, seeds the door's
// answer, which the mocks serve first.

const DESCRIBE = /^\/management\/v1\/table\/([^/]+)\/contract\/describe$/;

export function contractDoor(req: Request, url: URL): Response | null {
	const match = DESCRIBE.exec(url.pathname);
	if (!match || req.method !== 'POST') return null;
	const table = decodeURIComponent(match[1] ?? '');
	return Response.json({
		table,
		uuid: `uuid-${table}`,
		contract: null,
		set_by: null,
		set_at: null,
	});
}
