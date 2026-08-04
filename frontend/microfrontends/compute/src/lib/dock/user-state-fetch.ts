/**
 * The dock stores speak `fetch(endpoint)` / `fetch(endpoint, {method:'PUT'})`; this zone's user-state
 * plane is REMOTE FUNCTIONS (the transport ruling: JSON values ride `query`/`command`, and the old
 * `capi/v1/user-state/[document]` proxy is gone). This adapter is the seam — it maps the store's two
 * calls onto `readUserStateDoc`/`writeUserStateDoc` and answers with a real `Response`, so the
 * store's THREE outcomes (ok / absent / unreadable) survive unchanged: a status is a status.
 */
import { readUserStateDoc, writeUserStateDoc } from '$lib/remote/user-state.remote';

/** The two documents this zone's dock owns. */
type UserStateDocument = 'dock-layout' | 'dock-layout-library';

const asJSON = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

export function userStateFetcher(document: UserStateDocument): typeof fetch {
	// Bun's `typeof fetch` carries `preconnect`; the stores only ever call the function itself, so the
	// shim declares the call signature and is cast at the boundary rather than faking a static method.
	const call = async (_input: URL | RequestInfo, init?: RequestInit): Promise<Response> => {
		if (init?.method === 'PUT') {
			const value: unknown = JSON.parse(String(init.body ?? 'null'));
			const res = await writeUserStateDoc({ document, value });
			return res.ok
				? new Response(null, { status: 204 })
				: new Response(null, { status: res.status });
		}
		const res = await readUserStateDoc({ document });
		return res.ok ? asJSON(res.data) : new Response(null, { status: res.status });
	};
	return call as unknown as typeof fetch;
}
