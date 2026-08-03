import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	ProjectDetailSchema,
	ProjectListSchema,
	ProjectSchema,
	TaskListingSchema,
	type Project,
	type ProjectDetail,
	type ProjectList,
	type TaskListing,
} from '../types.js';

// The annotation-PROJECTS plane, in the zone's remote-function dialect (open_transport.md, area 4) —
// same `ApiResult` shapes at every call site, transport only. The `/api/projects/[...path]` proxy
// (GET+POST+PUT+DELETE) is deleted: its four verbs were a routing detail, and each surface it carried
// is a named function here.
//
// The confused-deputy stance is UNCHANGED. These run on the zone (SvelteKit/Bun) server and forward
// ONLY the signed-in user's bearer, so every FGA door the annotator service holds (can_manage,
// can_send_items, can_publish, can_claim, …) is still checked against a real user. The deleted route's
// `requireSession: true` on the write verbs is reproduced verbatim below: on an auth-enabled stack a
// signed-out write is refused HERE, with the same `{status: 401, detail: 'sign in required'}` the BFF
// answered, so a write is always attributable.
//
// PARSING moves, it is not invented: `client.ts` valibot-parsed every response at the browser
// boundary and those exact parses now run server-side, keeping the same failure shape
// (`status: 0`, `contract drift: …`) the pages already render as "unreachable".
//
// SINGLE-FLIGHT REFRESH, only where it is the sole update path: `createProject` refreshes the tenant
// list it just changed. The event/adjudication/send commands deliberately do NOT refresh — the detail
// page rebuilds ONE snapshot (project + task details together) after every action, on purpose, and a
// second refresh here would fan out a duplicate two-read snapshot per click and race that latest-wins
// sequencing.
//
// A remote file may export only remote functions, so the wire contracts stay in `../types.js`.

// Dev seam, carried over from the deleted route: the projects/task plane may live on a DIFFERENT
// annotator than the media plane (cluster actors + a locally-seeded corpus). Falls back to
// ANNOTATOR_API — one service in any real deploy.
const ANNOTATOR_API = env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';

const enc = encodeURIComponent;

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** The deleted route's `requireSession: true`: on an auth-enabled stack a write never leaves this
 *  server without a signed-in user, and the caller sees the same 401 it always did. */
function signedOut(): boolean {
	const { locals } = getRequestEvent();
	return locals.authEnabled && !locals.session;
}

const SIGN_IN_REQUIRED: ApiResult<never> = {
	ok: false,
	status: 401,
	detail: 'sign in required',
};

/** One annotator-service call → `ApiResult<unknown>`; FastAPI's `{detail}` is surfaced as the failure
 *  detail, exactly as the browser client lifted it out of the proxied body. An UNREACHABLE service is
 *  `{ok:false, status:0}` — a rejected fetch here would throw across the remote boundary and skip
 *  every consumer's honest offline branch ("The annotation service is unreachable."). */
async function annotatorJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	let res: Response;
	try {
		res = await fetch(`${ANNOTATOR_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
	if (!res.ok) {
		return {
			ok: false,
			status: res.status,
			detail: typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`,
		};
	}
	return { ok: true, data: body };
}

/** The boundary parse `client.ts` ran in the browser, moved here unchanged — including its status:
 *  a drift is `status: 0`, which the pages already read as "unreachable" rather than as a refusal. */
function parsed<T>(
	schema: v.BaseSchema<unknown, T, v.BaseIssue<unknown>>,
	result: ApiResult<unknown>,
): ApiResult<T> {
	if (!result.ok) return result;
	const decoded = v.safeParse(schema, result.data);
	if (!decoded.success) {
		return {
			ok: false,
			status: 0,
			detail: `contract drift: ${decoded.issues[0]?.message ?? 'decode failed'}`,
		};
	}
	return { ok: true, data: decoded.output };
}

/** A JSON write, gated by the session guard above. */
const write = (
	method: 'POST' | 'PUT' | 'DELETE',
	path: string,
	body?: unknown,
): Promise<ApiResult<unknown>> =>
	signedOut()
		? Promise.resolve(SIGN_IN_REQUIRED)
		: annotatorJSON(path, {
				method,
				...(body === undefined ? {} : { body: JSON.stringify(body) }),
			});

const ProjectIdArg = v.object({ projectId: v.string() });

/** The create body — the same optional-everything shape the dialog builds. Mirrors the service's
 *  `CreateProjectRequest`; the template's defaults live server-side, so an absent key is not `null`. */
const CreateProjectSchema = v.object({
	tenant: v.string(),
	slug: v.string(),
	title: v.optional(v.string()),
	description: v.optional(v.string()),
	instructions: v.optional(v.string()),
	review_required: v.optional(v.boolean()),
	lease_seconds: v.optional(v.number()),
	consensus_n: v.optional(v.number()),
	// Task templates v1: picked at create, ENFORCED server-side at submit.
	template: v.optional(
		v.object({
			kind: v.string(),
			tools: v.optional(v.array(v.string())),
			required_labels: v.optional(v.array(v.string())),
			attributes: v.optional(
				v.array(
					v.object({
						name: v.string(),
						type: v.optional(v.string()),
						choices: v.optional(v.array(v.string())),
						required: v.optional(v.boolean()),
					}),
				),
			),
			enforce: v.optional(v.boolean()),
		}),
	),
	label_schema: v.optional(
		v.object({
			classes: v.array(
				v.object({ name: v.string(), shape_types: v.optional(v.array(v.string())) }),
			),
			attributes: v.optional(v.array(v.string())),
		}),
	),
});

/** One item sent into a labeling task: where it comes from, and how it is displayed. */
const SendItemSchema = v.object({
	source: v.object({
		kind: v.string(),
		keys: v.array(v.string()),
		where: v.optional(v.nullable(v.string())),
	}),
	media: v.object({
		kind: v.string(),
		image_url: v.optional(v.nullable(v.string())),
		media_url: v.optional(v.nullable(v.string())),
	}),
});

// ── reads ──────────────────────────────────────────────────────────────────────────────────────

/** Every labeling task in the tenant, with its state and progress counts (the landing). */
export const listProjects = query(
	v.object({ tenant: v.string() }),
	async ({ tenant }): Promise<ApiResult<ProjectList>> =>
		parsed(ProjectListSchema, await annotatorJSON(`/projects?tenant=${enc(tenant)}`)),
);

/** One labeling task plus the transitions the CALLER may fire (`legal_events`, derived from the
 *  service's own machine tables — the UI never holds a second copy of the state machine). */
export const fetchProject = query(
	ProjectIdArg,
	async ({ projectId }): Promise<ApiResult<ProjectDetail>> =>
		parsed(ProjectDetailSchema, await annotatorJSON(`/projects/${projectId}`)),
);

/** The work queue: states + counts, and with `details` the full task documents the queue renders. */
export const listTasks = query(
	v.object({ projectId: v.string(), details: v.optional(v.boolean()) }),
	async ({ projectId, details }): Promise<ApiResult<TaskListing>> =>
		parsed(
			TaskListingSchema,
			await annotatorJSON(`/projects/${projectId}/tasks${details ? '?include=details' : ''}`),
		),
);

// ── writes ─────────────────────────────────────────────────────────────────────────────────────

/** Create a labeling task (born `draft`). Refreshes the tenant list in the same flight — the landing
 *  re-reads too, but this is the write that changes WHICH projects exist. */
export const createProject = command(
	CreateProjectSchema,
	async (req): Promise<ApiResult<Project>> => {
		const result = parsed(ProjectSchema, await write('POST', '/projects', req));
		if (result.ok) void listProjects({ tenant: req.tenant }).refresh();
		return result;
	},
);

/** Fire a project transition (open / freeze / publish / archive). `targetNamespace` rides only when
 *  the caller named one — publish pins its target, and an absent key is not the same as `null`. */
export const fireProjectEvent = command(
	v.object({
		projectId: v.string(),
		event: v.string(),
		targetNamespace: v.optional(v.string()),
	}),
	async ({ projectId, event, targetNamespace }): Promise<ApiResult<Project>> =>
		parsed(
			ProjectSchema,
			await write('POST', `/projects/${projectId}/events`, {
				event,
				...(targetNamespace ? { target_namespace: targetNamespace } : {}),
			}),
		),
);

/** Send items into a labeling task — each becomes a claimable item (×`consensus_n` replicas). */
export const sendItems = command(
	v.object({ projectId: v.string(), items: v.array(SendItemSchema) }),
	async ({
		projectId,
		items,
	}): Promise<ApiResult<{ sent: number; created: number; task_ids: string[] }>> =>
		parsed(
			v.object({ sent: v.number(), created: v.number(), task_ids: v.array(v.string()) }),
			await write('POST', `/projects/${projectId}/items`, { items }),
		),
);

/** Consensus v1's merge step: name one accepted replica of a group canonical (a pick, never a
 *  blend). PUT — re-picking while the project is adjudicable is the intended shape. */
export const adjudicate = command(
	v.object({ projectId: v.string(), groupId: v.string(), taskId: v.string() }),
	async ({ projectId, groupId, taskId }): Promise<ApiResult<Project>> =>
		parsed(
			ProjectSchema,
			await write('PUT', `/projects/${projectId}/adjudications/${enc(groupId)}`, {
				task_id: taskId,
			}),
		),
);

/** Withdraw a group's pick. Exists because the publish refuses a stale pick — without removal one
 *  wrong pick would wedge the publish permanently. Body-less, exactly as the DELETE proxy forwarded. */
export const clearAdjudication = command(
	v.object({ projectId: v.string(), groupId: v.string() }),
	async ({ projectId, groupId }): Promise<ApiResult<Project>> =>
		parsed(
			ProjectSchema,
			await write('DELETE', `/projects/${projectId}/adjudications/${enc(groupId)}`),
		),
);
