import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import {
	AccessCheckSchema,
	AccessGrantSchema,
	AccessGraphSchema,
	AccessListSchema,
	type AccessCheck,
	type AccessGrant,
	type AccessGraph,
	type AccessKind,
	type AccessList,
} from '../namespace';

// PER-OBJECT access (a table or a namespace), in the zone's remote-function dialect
// (open_transport.md, area 1) — same names, same ApiResult shapes as the /capi client this replaces,
// transport only. This is the OBJECT-scoped surface the catalog mounts under
// `/v1/{table|namespace}/{id}/access/*`; the estate-wide FGA workbench (`lib/admin/access*`) is a
// different surface with its own module.
//
// Kind-generalized exactly as the /capi client already was: one code path targets either FGA object
// type, and the owner-tier gate is the per-type relation (can_drop for a table, can_delete for a
// namespace), enforced by the CATALOG against the signed-in user's own bearer — the confused-deputy
// stance the deleted routes carried, kept intact (no service credential is ever forwarded).
// `fetchAccessGraph` absorbs the former table-only `fetchAccessGraph` and `fetchNamespaceAccessGraph`,
// which were the same request with a different path segment.
//
// A remote file may export only remote functions, so the wire contracts stay in `../namespace`.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

const enc = encodeURIComponent;

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
	// offline/status-0 branch (e.g. failText's "catalog unreachable / may-or-may-not-have-applied").
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

/** The client module's `AccessKind` as a wire option list — `satisfies` keeps the two from drifting. */
const KINDS = ['table', 'namespace'] as const satisfies readonly AccessKind[];

/** The FGA object an access call targets — `kind` picks the catalog surface, never the gate. */
const TargetSchema = v.object({
	kind: v.picklist(KINDS),
	id: v.string(),
});

/** A target plus the (subject, rung) pair a check/grant/revoke carries in its body. */
const SubjectSchema = v.object({
	kind: v.picklist(KINDS),
	id: v.string(),
	user: v.string(),
	relation: v.string(),
});

/** #51 access review, kind-generalized: who holds which can_* action on the object. Owner-gated by
 *  the catalog (can_drop / can_delete). POST because the catalog's lance-namespace-style surface is
 *  POST-shaped; the operation itself is a read, so it is a query here. */
export const fetchAccess = query(
	TargetSchema,
	async ({ kind, id }): Promise<ApiResult<AccessList>> =>
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/list`, { method: 'POST' }),
			AccessListSchema,
		),
);

/** #68 "who can do what" simulator — a live OpenFGA Check: does `user` hold `relation` on this
 *  object? Owner-gated by the catalog, the same bar as the review (probing the graph == disclosing it). */
export const checkAccess = query(
	SubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessCheck>> =>
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/check`, {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessCheckSchema,
		),
);

/** #81 one hop of the authorization graph around the object — its direct grantees + the parent edge.
 *  Owner-gated by the catalog; the SvelteFlow explorer and the namespace page's list card read it. */
export const fetchAccessGraph = query(
	TargetSchema,
	async ({ kind, id }): Promise<ApiResult<AccessGraph>> =>
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/graph`, { method: 'POST' }),
			AccessGraphSchema,
		),
);

/** #72 grant a base rung (owner/writer/reader/validator) to a subject on the object. `user` may be a
 *  bare id (`alice`) or a userset (`role:…#assignee` / `team:…#member`). On success the object's
 *  active review + graph refresh in the SAME flight, so a surface that issued a grant never renders
 *  an ACL it just changed. */
export const grantAccess = command(
	SubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessGrant>> => {
		const result = parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/grant`, {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessGrantSchema,
		);
		if (result.ok) {
			void fetchAccess({ kind, id }).refresh();
			void fetchAccessGraph({ kind, id }).refresh();
		}
		return result;
	},
);

/** #72 revoke a base rung from a subject on the object — the write counterpart of the grant, same
 *  gate, same single-flight refresh. */
export const revokeAccess = command(
	SubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessGrant>> => {
		const result = parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/revoke`, {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessGrantSchema,
		);
		if (result.ok) {
			void fetchAccess({ kind, id }).refresh();
			void fetchAccessGraph({ kind, id }).refresh();
		}
		return result;
	},
);
