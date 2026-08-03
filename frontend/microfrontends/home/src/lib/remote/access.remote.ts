import { command, getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import { TupleConditionSchema, TupleSchema, type Tuple } from '../access';

// The raw FGA write door, in the estate's remote-function dialect — the SAME command the lakehouse
// zone's FGA workbench writes with (`admin/remote/access.remote.ts`), so a tuple minted by the
// project-creation flow is validated, audited and control-event-emitting exactly like one granted
// from the workbench. One write door, not two.
//
// Only the WRITE is here: this zone grants a new project's initial admin and nothing else. The
// workbench's reads (store, check, expand, simulate …) stay in the lakehouse, where the canvas that
// renders them lives.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** One catalog call → `ApiResult<unknown>`; FastAPI's `{detail}` is surfaced as the failure detail.
 *  An UNREACHABLE catalog is `{ok:false, status:0}` — a rejected fetch must never cross the remote
 *  boundary, or the caller's honest offline branch is replaced by a boundary error. */
async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	let res: Response;
	try {
		res = await fetch(`${CATALOG_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	if (!res.ok) {
		let detail = `catalog answered ${res.status}`;
		try {
			const body: unknown = await res.json();
			if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
		} catch {
			/* a non-JSON error body keeps the status-line detail */
		}
		return { ok: false, status: res.status, detail };
	}
	return { ok: true, data: await res.json() };
}

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
function parsed<T>(result: ApiResult<unknown>, schema: v.GenericSchema<unknown, T>): ApiResult<T> {
	if (!result.ok) return result;
	try {
		return { ok: true, data: parse(schema, result.data) };
	} catch (err) {
		return { ok: false, status: 502, detail: `catalog contract drift: ${String(err)}` };
	}
}

/** What a CALLER supplies to write a tuple.
 *
 *  `condition` is optional here but always stated on a read-back `Tuple`, and that asymmetry is
 *  deliberate: a tuple READ from the store must say whether it is conditional (`null` is a real
 *  answer — "permanent"), while a caller writing a permanent grant should not have to say
 *  `condition: null` to mean "no". */
const TupleInputSchema = v.object({
	user: v.string(),
	relation: v.string(),
	object: v.string(),
	condition: v.optional(v.nullable(TupleConditionSchema)),
});

/** Grant: write one raw tuple. Estate-admin gated by the catalog.
 *
 *  No `.refresh()` (the lakehouse original single-flights its `fetchStore()`): this zone renders no
 *  tuple store, and the one surface a grant changes here — the project gallery — is a page load the
 *  create dialog invalidates at the call site. */
export const writeTuple = command(
	TupleInputSchema,
	async (tuple): Promise<ApiResult<Tuple>> =>
		parsed(
			await catalogJSON('/v1/access/tuples', { method: 'POST', body: JSON.stringify(tuple) }),
			TupleSchema,
		),
);
