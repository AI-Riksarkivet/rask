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

/** `DeleteProjectResponse` — what the retirement REALLY removed. `tuples_revoked` is reported rather
 *  than assumed (a `0` on an FGA-off stack is a fact, not a silent success), which is why the success
 *  toast is written from this body instead of from the request. */
const DeleteProjectSchema = v.object({
	project: v.string(),
	tuples_revoked: v.number(),
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

/** One control event, as `/v1/events` reports it — the fields the project page renders. */
const ProjectEventSchema = v.object({
	event_id: v.string(),
	occurred_at: v.string(),
	action: v.string(),
	object_type: v.string(),
	object_id: v.string(),
	actor: v.nullable(v.string()),
});
const EventsPageSchema = v.object({ events: v.array(ProjectEventSchema) });
export type ProjectEvent = v.InferOutput<typeof ProjectEventSchema>;

/** The 10 most recent control events touching ONE project (#71) — its own record plus its
 *  warehouses. Scope is honest about its reach: namespace/table events cannot be mapped to a
 *  project without walking the whole hierarchy per event, so this shows the governance rungs the
 *  page itself renders (project + warehouses); the estate-wide, filterable stream stays one link
 *  away at /lakehouse/admin/events. The events feed is estate-admin gated at the catalog — the
 *  page renders the 403 as its own quiet state rather than an error. */
export const projectEvents = query(
	v.string(),
	async (project): Promise<ApiResult<ProjectEvent[]>> => {
		const rec = await catalogJSON(`/v1/projects/${enc(project)}`);
		const mine = new Set<string>([`project:${project}`]);
		if (rec.ok) {
			const parsedRec = v.safeParse(ProjectSchema, rec.data);
			if (parsedRec.success)
				for (const w of parsedRec.output.warehouses) mine.add(`warehouse:${w.id}`);
		}
		const res = await catalogJSON('/v1/events?since=0');
		if (!res.ok) return res;
		const parsedPage = v.safeParse(EventsPageSchema, res.data);
		if (!parsedPage.success)
			return { ok: false, status: 502, detail: 'catalog contract drift: events page' };
		const events = parsedPage.output.events
			.filter((e) => mine.has(e.object_id))
			.sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
			.slice(0, 10);
		return { ok: true, data: events };
	},
);

/** Register a project — `POST /v1/projects`. The FIRST rung of the hierarchy, and it must be climbed
 *  explicitly: the catalog refuses a warehouse whose project has no registry record with a 404 that
 *  says so verbatim ("A warehouse must belong to an existing project (project > warehouse > namespace
 *  > table) — create it first"). A warehouse create no longer mints its project as a side effect, so
 *  anything provisioning a NEW project calls this first. */
export const createProject = command(
	v.object({ id: v.string(), name: v.optional(v.nullable(v.string())) }),
	async (body): Promise<ApiResult<unknown>> =>
		catalogJSON('/v1/projects', { method: 'POST', body: JSON.stringify(body) }),
);

/** Provision a warehouse under an EXISTING project. Project-admin gated by the catalog
 *  (can_create_warehouse); a brand-new project has no tuples, so the estate-admin door opens once and
 *  the catalog seeds the caller as the new project's admin.
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

/** Retire a tenant — `DELETE /v1/projects/{id}`. Gated at the catalog on the project's OWN
 *  `can_administer` bar, and every refusal is a state the dialog renders rather than an exception:
 *
 *  · **409** is the interesting one, and it comes in two flavours the `detail` distinguishes. Either the
 *    project still holds warehouses — the detail NAMES them, and they are the caller's next click — or
 *    the record carries the `protected` flag, which `force` overrides.
 *  · **404** is deliberately indistinguishable from "not yours": the delete door refuses to be an
 *    existence oracle for the estate's tenants, so the UI must not invent a distinction either.
 *
 *  **`force` is the ONLY parameter, and there is no `cascade` on this route at all.** A project cascade
 *  would reach warehouses, and a warehouse's own delete can purge a bucket — one request must never be
 *  able to destroy a tenant's storage transitively. Emptying goes one rung at a time through
 *  `DELETE /v1/warehouses/{id}`, which is exactly what the 409's blocker links point at.
 *
 *  No `.refresh()`: the thing this deletes is what `fetchProject` reads, so refreshing it would re-read a
 *  project that no longer exists purely to watch it 404. The caller navigates back to `/projects`
 *  instead, and that load re-reads the estate. */
export const deleteProject = command(
	v.object({
		project: v.string(),
		/** Overrides the registry record's deletion-PROTECTION flag ONLY — the FGA gate ran first and runs
		 *  identically with or without it, so forcing cannot delete a project the caller may not
		 *  administer. Omitted (not `false`) unless the caller opted in. */
		force: v.optional(v.boolean()),
	}),
	async ({ project, force }): Promise<ApiResult<{ project: string; tuples_revoked: number }>> =>
		parsed(
			await catalogJSON(`/v1/projects/${enc(project)}${force ? '?force=true' : ''}`, {
				method: 'DELETE',
			}),
			DeleteProjectSchema,
		),
);
