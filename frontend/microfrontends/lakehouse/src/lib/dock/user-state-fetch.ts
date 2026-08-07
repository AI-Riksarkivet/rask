/**
 * The dock stores speak `fetch(endpoint)` / `fetch(endpoint, {method:'PUT'})`; this zone's user-state
 * plane is REMOTE FUNCTIONS (the transport ruling: JSON values ride `query`/`command`, and the old
 * `capi/v1/user-state/[document]` proxy is gone). This adapter is the seam — it maps the store's two
 * calls onto `readUserStateDoc`/`writeUserStateDoc` and answers with a real `Response`, so the
 * store's THREE outcomes (ok / absent / unreadable) survive unchanged: a status is a status.
 */
import { readUserStateDoc, writeUserStateDoc } from '$lib/data/remote/catalog.remote';

/** The two documents this zone's dock owns. */
type UserStateDocument = 'dock-layout' | 'dock-layout-library';

const asJSON = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** An `ApiResult` failure status as a RESPONSE status. `0` is the union's own "unreachable" — a
 *  transport fact, not an HTTP status — and `new Response(null, {status: 0})` throws RangeError.
 *  Until #93 unified the transports this path could not actually receive a 0 (this zone's copies
 *  were the 502 deviants), so the crash was armed the moment the statuses were standardised. 503 is
 *  the honest HTTP spelling of "the thing behind me is not answering", and the dock store treats
 *  every 5xx as its `unreadable` outcome, which is exactly right for it. */
const asResponseStatus = (status: number): number => (status === 0 ? 503 : status);

export function userStateFetcher(document: UserStateDocument): typeof fetch {
	// Bun's `typeof fetch` carries `preconnect`; the stores only ever call the function itself, so the
	// shim declares the call signature and is cast at the boundary rather than faking a static method.
	const call = async (_input: URL | RequestInfo, init?: RequestInit): Promise<Response> => {
		if (init?.method === 'PUT') {
			const value: unknown = JSON.parse(String(init.body ?? 'null'));
			const res = await writeUserStateDoc({ document, value });
			return res.ok
				? new Response(null, { status: 204 })
				: new Response(null, { status: asResponseStatus(res.status) });
		}
		const res = await readUserStateDoc({ document });
		return res.ok ? asJSON(res.data) : new Response(null, { status: asResponseStatus(res.status) });
	};
	return call as unknown as typeof fetch;
}
