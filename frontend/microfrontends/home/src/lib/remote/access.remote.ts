import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { fetchMe, parse, type Me } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import {
	AccessModelSchema,
	CheckVerdictSchema,
	ExpandResultSchema,
	ListObjectsResultSchema,
	ListUsersResultSchema,
	SimulateResultSchema,
	TupleConditionSchema,
	TupleSchema,
	TuplesPageSchema,
	type AccessModel,
	type CheckVerdict,
	type ExpandResult,
	type ListObjectsResult,
	type ListUsersResult,
	type SimulateResult,
	type Tuple,
	type TuplesPage,
} from '../access';

// The FGA workbench's data plane, as REMOTE FUNCTIONS — the estate's dialect. The functions run on the
// zone (SvelteKit/Bun) server, reach the catalog directly with the signed-in session's bearer, and PARSE
// at the wire boundary — which lives here, not in the browser — so a contract drift is an `{ok:false}`
// the component renders honestly, never a cast.
//
// Results are the shared `ApiResult<T>` union rather than thrown `error()`s, on the dock-layout
// precedent (`layout.remote.ts`'s ok/absent/unreadable): the explorer renders DIFFERENT states off the
// status (401/403 → "estate-admin only", else the detail), and a discriminated union keeps that an
// exhaustive branch instead of exception control flow.
//
// ONE write door, not two: `writeTuple` is what both the workbench's Grant dialog and the
// project-creation flow's initial-admin grant go through, so a tuple minted by either is validated,
// audited and control-event-emitting identically.
//
// A remote file may export only remote functions, so the wire contracts stay in `../access`.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

/** Whole-store page ceiling, moved here from the Explorer with the loop itself: 40 × 100 = 4000 tuples
 *  on the canvas — past that the graph is unreadable and the honest answer is `truncated: true`, never
 *  silent clipping. Server-side the loop is one browser round trip instead of forty. */
const MAX_GRAPH_PAGES = 40;

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** One catalog call → `ApiResult<unknown>`; FastAPI's `{detail}` is surfaced as the failure detail. */
async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	// An UNREACHABLE catalog is `{ok:false, status:0}`, exactly like the browser client this replaced —
	// a rejected fetch here would throw across the remote boundary and skip every consumer's honest
	// offline/status-0 branch.
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

/** What a CALLER supplies to write/delete/simulate.
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

const TupleFilterSchema = v.object({
	objectType: v.optional(v.string()),
	user: v.optional(v.string()),
	object: v.optional(v.string()),
	pageSize: v.optional(v.number()),
	continuation: v.optional(v.string()),
});

/** One filtered page of tuples (the Inspector's per-object read). */
export const fetchTuples = query(
	TupleFilterSchema,
	async (filter): Promise<ApiResult<TuplesPage>> => {
		const q = new URLSearchParams();
		if (filter.objectType) q.set('object_type', filter.objectType);
		if (filter.user) q.set('user', filter.user);
		if (filter.object) q.set('object', filter.object);
		if (filter.pageSize) q.set('page_size', String(filter.pageSize));
		if (filter.continuation) q.set('continuation', filter.continuation);
		const qs = q.toString();
		return parsed(await catalogJSON(`/v1/access/tuples${qs ? `?${qs}` : ''}`), TuplesPageSchema);
	},
);

/** The WHOLE tuple store — the canvas's resting graph. The page loop runs here. */
export const fetchStore = query(
	async (): Promise<ApiResult<{ tuples: Tuple[]; truncated: boolean }>> => {
		const tuples: Tuple[] = [];
		let continuation: string | null = null;
		for (let page = 0; page < MAX_GRAPH_PAGES; page++) {
			const q = new URLSearchParams({ page_size: '100' });
			if (continuation) q.set('continuation', continuation);
			const result = parsed(await catalogJSON(`/v1/access/tuples?${q}`), TuplesPageSchema);
			if (!result.ok) return result;
			tuples.push(...result.data.tuples);
			continuation = result.data.continuation;
			if (!continuation) return { ok: true, data: { tuples, truncated: false } };
		}
		return { ok: true, data: { tuples, truncated: true } };
	},
);

/** Grant: write one raw tuple. Estate-admin gated by the catalog; on success the active `fetchStore`
 *  instances are refreshed in the SAME flight (single-flight mutation), so the canvas that issued the
 *  grant never renders a store it just changed. The project-creation flow writes through this same
 *  command; there the refresh is a no-op (no store is mounted) and the gallery is invalidated at the
 *  call site instead. */
export const writeTuple = command(TupleInputSchema, async (tuple): Promise<ApiResult<Tuple>> => {
	const result = parsed(
		await catalogJSON('/v1/access/tuples', { method: 'POST', body: JSON.stringify(tuple) }),
		TupleSchema,
	);
	if (result.ok) void fetchStore().refresh();
	return result;
});

/** Revoke: delete one raw tuple (the tuple IS the identity — same body as the write). */
export const deleteTuple = command(TupleInputSchema, async (tuple): Promise<ApiResult<Tuple>> => {
	const result = parsed(
		await catalogJSON('/v1/access/tuples', { method: 'DELETE', body: JSON.stringify(tuple) }),
		TupleSchema,
	);
	if (result.ok) void fetchStore().refresh();
	return result;
});

/** A live OpenFGA Check. `context` supplies the operands a CONDITION evaluates against
 *  (`current_time`); an EMPTY context is omitted entirely — OpenFGA reads `{}` as "no operands",
 *  which ERRORS any CEL expression on the path instead of evaluating it. */
export const checkAccess = query(
	v.object({
		user: v.string(),
		relation: v.string(),
		object: v.string(),
		context: v.optional(v.record(v.string(), v.unknown())),
	}),
	async ({ context, ...tuple }): Promise<ApiResult<CheckVerdict>> =>
		parsed(
			await catalogJSON('/v1/access/check', {
				method: 'POST',
				body: JSON.stringify(
					context && Object.keys(context).length ? { ...tuple, context } : tuple,
				),
			}),
			CheckVerdictSchema,
		),
);

/** The authorization model (read-only — model changes are code migrations). */
export const fetchAccessModel = query(
	async (): Promise<ApiResult<AccessModel>> =>
		parsed(await catalogJSON('/v1/access/model'), AccessModelSchema),
);

/** The catalog registry (`<ns>$<table>` ids) — one-click graph seeds. */
export const fetchTables = query(
	async (): Promise<ApiResult<{ tables: string[] }>> =>
		parsed(await catalogJSON('/v1/table'), v.object({ tables: v.array(v.string()) })),
);

/** The signed-in identity — what the tuple store actually keys on. Fail-soft to `null` (no session,
 *  catalog down): the "me" chip simply does not render, exactly like the shell's own use of fetchMe. */
export const accessMe = query(async (): Promise<Me | null> => {
	const { locals, fetch } = getRequestEvent();
	return fetchMe({
		catalogUrl: CATALOG_API,
		accessToken: locals.session?.accessToken,
		fetchFn: fetch,
	});
});

/** "What can this subject reach?" — every `type` object the subject holds `relation` on. */
export const listObjects = query(
	v.object({ user: v.string(), relation: v.string(), type: v.string() }),
	async (body): Promise<ApiResult<ListObjectsResult>> =>
		parsed(
			await catalogJSON('/v1/access/list-objects', { method: 'POST', body: JSON.stringify(body) }),
			ListObjectsResultSchema,
		),
);

/** "Who can do this?" — the effective subject set on one object. */
export const listUsers = query(
	v.object({
		object: v.string(),
		relation: v.string(),
		userType: v.optional(v.string()),
		userRelation: v.optional(v.nullable(v.string())),
	}),
	async (body): Promise<ApiResult<ListUsersResult>> =>
		parsed(
			await catalogJSON('/v1/access/list-users', {
				method: 'POST',
				body: JSON.stringify({
					object: body.object,
					relation: body.relation,
					user_type: body.userType ?? 'user',
					user_relation: body.userRelation ?? null,
				}),
			}),
			ListUsersResultSchema,
		),
);

/** "Why does this resolve?" — the derivation tree, followed server-side to `depth`. */
export const expand = query(
	v.object({ object: v.string(), relation: v.string(), depth: v.optional(v.number()) }),
	async (body): Promise<ApiResult<ExpandResult>> =>
		parsed(
			await catalogJSON('/v1/access/expand', {
				method: 'POST',
				body: JSON.stringify({
					object: body.object,
					relation: body.relation,
					depth: body.depth ?? 1,
				}),
			}),
			ExpandResultSchema,
		),
);

/** Assert before you grant: does the proposed tuple change the answer? Writes nothing. */
export const simulate = query(
	v.object({
		user: v.string(),
		relation: v.string(),
		object: v.string(),
		hypothetical: v.array(TupleInputSchema),
	}),
	async (body): Promise<ApiResult<SimulateResult>> =>
		parsed(
			await catalogJSON('/v1/access/simulate', { method: 'POST', body: JSON.stringify(body) }),
			SimulateResultSchema,
		),
);
