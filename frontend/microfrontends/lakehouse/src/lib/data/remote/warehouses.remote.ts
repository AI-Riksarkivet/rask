import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import type { CreateWarehouseBody, ProjectSummary, WarehouseRecord } from '../catalog';

// The warehouse + project registry, in the zone's remote-function dialect — same
// names, same `ApiResult` shapes as the /capi client this replaces, transport only. The three narrow
// BFF routes it retires (`capi/v1/warehouses`, `capi/v1/warehouses/[id]/[action]`) existed to keep the
// catalog's project-admin gate enforced against a REAL user rather than a service credential; that
// stance is unchanged here — a remote function runs on the zone server and forwards only the signed-in
// session's bearer, and the action allowlist dissolves into three named commands.
//
// The routes' `authEnabled && !session → 401` short-circuit is gone deliberately: it was a UX
// shortcut, never the enforcement point (the catalog answers 401 itself for a bearer-less call), and
// the components branch on the STATUS, which is identical either way.

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

async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	try {
		const res = await fetch(`${CATALOG_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
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
	} catch (err) {
		// The routes this replaces answered an unreachable catalog with 502 rather than throwing, and
		// the pages read that status to render "Catalog unreachable" — an uncaught fetch failure would
		// replace that honest state with a boundary error.
		return { ok: false, status: 502, detail: String(err) };
	}
}

function parsed<T>(result: ApiResult<unknown>, schema: v.GenericSchema<unknown, T>): ApiResult<T> {
	if (!result.ok) return result;
	try {
		return { ok: true, data: parse(schema, result.data) };
	} catch (err) {
		return { ok: false, status: 502, detail: `catalog contract drift: ${String(err)}` };
	}
}

/** Warehouse admin reads: whatever the catalog shows this caller (any signed-in user). */
export const fetchWarehouses = query(
	async (): Promise<ApiResult<WarehouseRecord[]>> =>
		parsed(await catalogJSON('/v1/warehouses'), v.array(WarehouseSchema)),
);

/** One warehouse record — the hierarchy drill-down's warehouse page (can_get_metadata gated). */
export const fetchWarehouse = query(
	v.string(),
	async (id): Promise<ApiResult<WarehouseRecord>> =>
		parsed(await catalogJSON(`/v1/warehouses/${enc(id)}`), WarehouseSchema),
);

/** The estate's tenants (estate-observer gated by the catalog — a member sees 403, handled). */
export const fetchProjects = query(
	async (): Promise<ApiResult<ProjectSummary[]>> =>
		parsed(await catalogJSON('/v1/projects'), v.array(ProjectSchema)),
);

/** One tenant: its warehouses + effective admins — the hierarchy drill-down's project page. */
export const fetchProject = query(
	v.string(),
	async (project): Promise<ApiResult<ProjectSummary>> =>
		parsed(await catalogJSON(`/v1/projects/${enc(project)}`), ProjectSchema),
);

/** Provision a warehouse — the create that MINTS a project when its `project` is new. Project-admin
 *  gated by the catalog (can_create_warehouse); on success both registry reads refresh in the same
 *  flight, so the gallery that issued the create never renders the estate it just changed. */
export const createWarehouse = command(
	v.object({
		id: v.string(),
		project: v.string(),
		bucket: v.optional(v.nullable(v.string())),
		serving: v.optional(v.literal('gold')),
	}),
	async (body: CreateWarehouseBody): Promise<ApiResult<WarehouseRecord>> => {
		const result = parsed(
			await catalogJSON('/v1/warehouses', { method: 'POST', body: JSON.stringify(body) }),
			WarehouseSchema,
		);
		if (result.ok) {
			void fetchWarehouses().refresh();
			void fetchProjects().refresh();
		}
		return result;
	},
);

/** The activate/deactivate lifecycle — one command instead of the route's action allowlist. */
export const setWarehouseActive = command(
	v.object({ id: v.string(), active: v.boolean() }),
	async ({ id, active }): Promise<ApiResult<WarehouseRecord>> => {
		const result = parsed(
			await catalogJSON(`/v1/warehouses/${enc(id)}/${active ? 'activate' : 'deactivate'}`, {
				method: 'POST',
			}),
			WarehouseSchema,
		);
		if (result.ok) {
			void fetchWarehouses().refresh();
			void fetchProjects().refresh();
		}
		return result;
	},
);

/** Bind a namespace to a warehouse (bucket-per-warehouse tenancy). The response is not rendered —
 *  the banner reports the outcome — so it is passed through unparsed. The registry read refreshes
 *  rather than nothing at all: an explicit single-flight refresh keeps SvelteKit from falling back to
 *  invalidating every query on the page. */
export const bindWarehouseNamespace = command(
	v.object({ id: v.string(), namespace: v.string() }),
	async ({ id, namespace }): Promise<ApiResult<unknown>> => {
		const result = await catalogJSON(`/v1/warehouses/${enc(id)}/namespaces`, {
			method: 'POST',
			body: JSON.stringify({ namespace }),
		});
		if (result.ok) void fetchWarehouses().refresh();
		return result;
	},
);
