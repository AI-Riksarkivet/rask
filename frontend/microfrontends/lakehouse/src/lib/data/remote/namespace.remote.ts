import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import { DropNamespaceResponseSchema, type DropNamespace } from '../catalog';
import { fetchTables } from './catalog.remote';
import {
	PolicyDeleteResponseSchema,
	PolicyResponseSchema,
	type NamespacePolicy,
	type NamespacePolicyDelete,
} from '../namespace';

// The NAMESPACE-scoped lifecycle + maintenance-policy surfaces, in the zone's remote-function dialect
// (the transport ruling, area 1) — same names, same ApiResult shapes as the /capi client this replaces,
// transport only. Owner-gated by the CATALOG (can_delete on namespace:<id>) against the signed-in
// user's own bearer, which is the confused-deputy stance the deleted routes carried.
//
// The valibot parse that used to run in the BROWSER (these responses are serialized with
// response_model_exclude_none, so nullable fields arrive absent) now runs at the wire boundary here:
// a contract drift is an honest `{ok:false, status:502}` the caller renders, never a cast.
//
// A remote file may export only remote functions, so the wire contracts stay in `../catalog` and
// `../namespace`.

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

/** The tables under ONE namespace — the namespace→table rung of the zone Overview's hierarchy view
 *  (#109). The spec's list_tables through the catalog's GET route, reader-gated like every other read
 *  here.
 *
 *  Deliberately NOT `catalog.remote.ts`'s `fetchTables`: that one reads the whole estate's registry in
 *  `<ns>$<table>` canonical form for the tables PAGE, so drawing a per-namespace rung from it would
 *  mean pulling every table in the estate to render one branch — and it would answer for namespaces
 *  this caller cannot read, which the hierarchy must show as "unreadable" rather than as empty. */
const NamespaceTablesSchema = v.object({ tables: v.array(v.string()) });
export const fetchNamespaceTables = query(
	v.string(),
	async (namespace): Promise<ApiResult<{ tables: string[] }>> =>
		parsed(await catalogJSON(`/v1/namespace/${enc(namespace)}/table/list`), NamespaceTablesSchema),
);

/** What a CALLER supplies to set a policy — the PolicyRequest wire body, every bound optional
 *  (absent = "the global default applies"), which is why the form omits an empty field rather than
 *  sending null. */
const PolicyDraftSchema = v.object({
	compact_enabled: v.optional(v.boolean(), true),
	compact_interval_hours: v.optional(v.nullable(v.number())),
	retain_versions: v.optional(v.nullable(v.number())),
	retention_days: v.optional(v.nullable(v.number())),
	target_rows_per_fragment: v.optional(v.nullable(v.number())),
});

/** The namespace's maintenance policy (reader-gated) — 404 when none is set, which the caller
 *  renders as the honest "no policy, global defaults apply" state (never an error). */
export const fetchNamespacePolicy = query(
	v.object({ namespace: v.string() }),
	async ({ namespace }): Promise<ApiResult<NamespacePolicy>> =>
		parsed(
			await catalogJSON(`/v1/namespace/${enc(namespace)}/policy/describe`, { method: 'POST' }),
			PolicyResponseSchema,
		),
);

/** Set (or replace) the namespace-level maintenance policy — it governs every dataset under the
 *  namespace's prefix unless a table policy overrides it. Owner-gated (can_delete); on success the
 *  active describe refreshes in the same flight. */
export const setNamespacePolicy = command(
	v.object({ namespace: v.string(), policy: PolicyDraftSchema }),
	async ({ namespace, policy }): Promise<ApiResult<NamespacePolicy>> => {
		const result = parsed(
			await catalogJSON(`/v1/namespace/${enc(namespace)}/policy/set`, {
				method: 'POST',
				body: JSON.stringify(policy),
			}),
			PolicyResponseSchema,
		);
		if (result.ok) void fetchNamespacePolicy({ namespace }).refresh();
		return result;
	},
);

/** Remove the namespace's maintenance policy (idempotent) — owner-gated, same single-flight refresh. */
export const deleteNamespacePolicy = command(
	v.object({ namespace: v.string() }),
	async ({ namespace }): Promise<ApiResult<NamespacePolicyDelete>> => {
		const result = parsed(
			await catalogJSON(`/v1/namespace/${enc(namespace)}/policy/delete`, { method: 'POST' }),
			PolicyDeleteResponseSchema,
		);
		if (result.ok) void fetchNamespacePolicy({ namespace }).refresh();
		return result;
	},
);

/** #85 drop a namespace. Cascade is a BODY field (`behavior: "Cascade"` also drops the tables inside;
 *  the default `"Restrict"` errors on a non-empty namespace) — not a query param. Owner-gated by the
 *  catalog (can_delete on namespace:<id>). */
export const dropNamespace = command(
	v.object({ namespace: v.string(), cascade: v.boolean() }),
	async ({ namespace, cascade }): Promise<ApiResult<DropNamespace>> => {
		const result = parsed(
			await catalogJSON(`/v1/namespace/${enc(namespace)}/drop`, {
				method: 'POST',
				body: JSON.stringify({ behavior: cascade ? 'Cascade' : 'Restrict' }),
			}),
			DropNamespaceResponseSchema,
		);
		// The registry IS the table list folded by namespace — a dropped namespace (and, on Cascade, its
		// tables) must leave it in the same flight, or the row the drop just removed re-renders intact.
		if (result.ok) void fetchTables().refresh();
		return result;
	},
);
