import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Table-detail aggregate (#52) — one BFF round-trip for the detail page instead of five browser
// calls against POST-shaped catalog reads the GET-only /capi catch-all cannot proxy. The server
// route makes fixed calls (no client body is ever forwarded), carries only the signed-in user's
// bearer (the promote-route confused-deputy stance), and the catalog's reader gate authorizes each
// upstream read.
//
// Absence vs failure is kept distinct per part: a 404 becomes `null` (genuinely unset — e.g. no
// policy), but a transient 5xx/403 becomes `{ error: status }` so the page can render "unavailable"
// instead of an affirmative "no policy" that would invite an overwriting write (audit 2026-07-16).
type Part = unknown | { error: number } | null;

export const GET: RequestHandler = async ({ params, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to view table details' }, { status: 401 });
	}
	const headers: Record<string, string> = {};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const base = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}`;
	const post = (path: string) => fetch(`${base}/${path}`, { method: 'POST', headers });
	const part = async (res: Response): Promise<Part> => {
		if (res.ok) return res.json();
		return res.status === 404 ? null : { error: res.status };
	};
	try {
		const describe = await post('describe?load_detailed_metadata=true&with_table_uri=true');
		if (!describe.ok) {
			// The primary read's status decides the whole page state — pass it through untouched.
			return new Response(describe.body, {
				status: describe.status,
				headers: { 'content-type': describe.headers.get('content-type') ?? 'application/json' },
			});
		}
		const [stats, versions, tags, branches, indexes, policy] = await Promise.all([
			post('stats').then(part),
			post('version/list').then(part),
			post('tags/list').then(part),
			post('branches/list').then(part),
			post('index/list').then(part),
			post('policy/describe').then(part),
		]);
		return json({
			describe: await describe.json(),
			stats,
			versions,
			tags,
			branches,
			indexes,
			policy,
			// #78 format is a fixed property of this catalog: it stores Lance datasets ONLY (columnar,
			// self-describing, versioned), pinned to storage format 2.2 at create (dataplane.py
			// data_storage_version="2.2"). Surfaced so the one supported format is discoverable, and the
			// create path 400s a client that tries to select another (write.format.default / data_source_format).
			format: { name: 'Lance', storage_version: '2.2' },
		});
	} catch (err) {
		console.error(`capi detail proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
