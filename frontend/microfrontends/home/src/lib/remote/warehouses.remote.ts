import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import type { CreateWarehouseBody, ProjectSummary, WarehouseRecord } from '../catalog';

// The project + warehouse registry, in the estate's remote-function dialect
// (docs/architecture/frontend-conventions.md §1.0) — the SAME names, valibot schemas and `ApiResult`
// shapes as the lakehouse zone's `data/remote/warehouses.remote.ts` this is lifted from. Only the
// SURFACE moved (the 2026-08-03 ruling: a project is the top of the hierarchy, so the estate's
// project list and one project's overview are main-menu pages); the transport is unchanged.
//
// A remote function runs on the zone server and forwards only the signed-in session's bearer, so the
// catalog's own project-admin gate is enforced against a REAL user rather than a service credential.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

const enc = encodeURIComponent;

/** `WarehouseResponse` + the additive `serving` class (see `WarehouseRecord` in ../catalog). */
const WarehouseSchema = v.object({
	id: v.string(),
	project: v.string(),
	bucket: v.string(),
	root_uri: v.string(),
	created_at: v.optional(v.nullable(v.string())),
	serving: v.optional(v.nullable(v.string())),
	status: v.optional(v.nullable(v.string())),
});

/** `ProjectResponse` — the tenant's warehouses (a narrower record than the registry's) + its
 *  effective admins. */
const ProjectSchema = v.object({
	project: v.string(),
	warehouses: v.array(
		v.object({
			id: v.string(),
			bucket: v.string(),
			serving: v.optional(v.nullable(v.string())),
			status: v.string(),
		}),
	),
	admins: v.array(v.string()),
});

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** One catalog call → `ApiResult<unknown>`; FastAPI's `{detail}` is surfaced as the failure detail.
 *
 *  An UNREACHABLE catalog is `{ok:false, status:0}` (§1.0: a rejected fetch must never cross the
 *  remote boundary) — the pages read that status to render an honest "Catalog unreachable" instead
 *  of a boundary error, and the create dialog's `failText` distinguishes a client TIMEOUT (where the
 *  catalog may still have committed) from a refused connection off the same status. */
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

/** One tenant: its warehouses + effective admins — what `/projects/[project]` renders. Gated by the
 *  catalog (401/403/404 are all honest states the page branches on). */
export const fetchProject = query(
	v.string(),
	async (project): Promise<ApiResult<ProjectSummary>> =>
		parsed(await catalogJSON(`/v1/projects/${enc(project)}`), ProjectSchema),
);

/** Provision a warehouse — the create that MINTS a project when its `project` is new. Project-admin
 *  gated by the catalog (can_create_warehouse); a brand-new project has no tuples, so the
 *  estate-admin door opens once and the catalog seeds the caller as the new project's admin.
 *
 *  No `.refresh()` here, unlike the lakehouse original: the gallery this create sits on is a page
 *  LOAD (`$lib/gallery`), not a `query()`, so there is nothing to single-flight — the dialog's
 *  `oncreated` calls `invalidateAll()` at the call site instead. Inventing a `query()` mirror of the
 *  load purely to have something to refresh would be the second implementation of the list this
 *  whole move exists to delete. */
export const createWarehouse = command(
	v.object({
		id: v.string(),
		project: v.string(),
		bucket: v.optional(v.nullable(v.string())),
		serving: v.optional(v.literal('gold')),
	}),
	async (body: CreateWarehouseBody): Promise<ApiResult<WarehouseRecord>> =>
		parsed(
			await catalogJSON('/v1/warehouses', { method: 'POST', body: JSON.stringify(body) }),
			WarehouseSchema,
		),
);
