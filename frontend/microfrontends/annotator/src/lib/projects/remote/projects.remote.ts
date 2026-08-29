import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	SIGN_IN_REQUIRED,
	parsed,
	projectsJSON as annotatorJSON,
	signedOut,
} from '$lib/server/doors';
import {
	MemberListSchema,
	ProjectDetailSchema,
	ProjectListSchema,
	SendItemSchema,
	ProjectSchema,
	TaskListingSchema,
	type MemberList,
	type Project,
	type ProjectDetail,
	type ProjectList,
	type TaskListing,
} from '../types.js';

// The annotation-PROJECTS plane, in the zone's remote-function dialect (the transport ruling, area 4) —
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
// boundary and those exact parses now run server-side. Transport and parse live in
// `$lib/server/doors` over `@rask/api/upstream` (#93) — a drift is a 502 with the drift's own
// words, and every page here buckets non-401/403/404 failures into its offline branch.
//
// SINGLE-FLIGHT REFRESH, only where it is the sole update path: `createProject` refreshes the tenant
// list it just changed. The event/adjudication/send commands deliberately do NOT refresh — the detail
// page rebuilds ONE snapshot (project + task details together) after every action, on purpose, and a
// second refresh here would fan out a duplicate two-read snapshot per click and race that latest-wins
// sequencing.
//
// A remote file may export only remote functions, so the wire contracts stay in `../types.js`.

const enc = encodeURIComponent;

/** A JSON write, gated by the session guard above. */
const write = (
	method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
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

/** The ontology as it rides the wire. Deliberately permissive about which keys are present — the
 *  SERVICE validates it as a unit, and mirroring those rules here would be a second source of truth
 *  for exactly the cross-checks this model exists to centralise. */
const OntologyWireSchema = v.object({
	kind: v.optional(v.string()),
	modality: v.optional(v.string()),
	classes: v.optional(
		v.array(
			v.object({
				name: v.string(),
				colour: v.optional(v.nullable(v.string())),
				tools: v.optional(v.array(v.string())),
				/** Regions of this class carry transcribed text (the row's `text` facet). */
				transcribe: v.optional(v.boolean()),
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
				required: v.optional(v.boolean()),
			}),
		),
	),
	relations: v.optional(
		v.array(
			v.object({
				name: v.string(),
				from_classes: v.optional(v.array(v.string())),
				to_classes: v.optional(v.array(v.string())),
				directed: v.optional(v.boolean()),
				required: v.optional(v.boolean()),
			}),
		),
	),
	allow_empty: v.optional(v.boolean()),
});

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
	// The whole task definition; absent = an unconstrained project. Parsed by the SERVICE's own
	// model, whose cross-checks (relations must reference declared classes, no duplicate names) had
	// nowhere to run while this was two independent fields.
	ontology: v.optional(OntologyWireSchema),
});

/** One item sent into a labeling task: where it comes from, and how it is displayed. */

// ── reads ──────────────────────────────────────────────────────────────────────────────────────

/** Every labeling task in the tenant, with its state and progress counts (the landing). */
export const listProjects = query(
	v.object({ tenant: v.string() }),
	async ({ tenant }): Promise<ApiResult<ProjectList>> =>
		parsed(await annotatorJSON(`/projects?tenant=${enc(tenant)}`), ProjectListSchema),
);

/** One labeling task plus the transitions the CALLER may fire (`legal_events`, derived from the
 *  service's own machine tables — the UI never holds a second copy of the state machine). */
export const fetchProject = query(
	ProjectIdArg,
	async ({ projectId }): Promise<ApiResult<ProjectDetail>> =>
		parsed(await annotatorJSON(`/projects/${projectId}`), ProjectDetailSchema),
);

/** Who holds which rung directly on this project. `can_manage`-gated server-side: who has access is
 *  itself sensitive, and a viewer has no business enumerating the people worth phishing. */
export const fetchMembers = query(
	ProjectIdArg,
	async ({ projectId }): Promise<ApiResult<MemberList>> =>
		parsed(await annotatorJSON(`/projects/${projectId}/members`), MemberListSchema),
);

/** Grant one rung. Idempotent server-side, so a double-click is a no-op rather than a 400. */
export const grantMember = command(
	v.object({ projectId: v.string(), user: v.string(), relation: v.string() }),
	async ({ projectId, user, relation }): Promise<ApiResult<MemberList>> => {
		const result = parsed(
			await write('PUT', `/projects/${projectId}/members`, { user, relation }),
			MemberListSchema,
		);
		if (result.ok) void fetchMembers({ projectId }).refresh();
		return result;
	},
);

/** Revoke one rung. The server REFUSES the last owner/manager — a project nobody can administer is
 *  not a state anyone can undo from inside the product, so the refusal arrives as a named 409.
 *  Query params, not a body: a DELETE body has no defined semantics and some intermediaries strip
 *  it, so the annotator service binds `user`+`relation` from the query string. */
export const revokeMember = command(
	v.object({ projectId: v.string(), user: v.string(), relation: v.string() }),
	async ({ projectId, user, relation }): Promise<ApiResult<MemberList>> => {
		const result = parsed(
			await write(
				'DELETE',
				`/projects/${projectId}/members?user=${enc(user)}&relation=${enc(relation)}`,
			),
			MemberListSchema,
		);
		if (result.ok) void fetchMembers({ projectId }).refresh();
		return result;
	},
);

/** The work queue: states + counts, and with `details` the full task documents the queue renders. */
export const listTasks = query(
	v.object({ projectId: v.string(), details: v.optional(v.boolean()) }),
	async ({ projectId, details }): Promise<ApiResult<TaskListing>> =>
		parsed(
			await annotatorJSON(`/projects/${projectId}/tasks${details ? '?include=details' : ''}`),
			TaskListingSchema,
		),
);

// ── writes ─────────────────────────────────────────────────────────────────────────────────────

/** Create a labeling task (born `draft`). Refreshes the tenant list in the same flight — the landing
 *  re-reads too, but this is the write that changes WHICH projects exist. */
export const createProject = command(
	CreateProjectSchema,
	async (req): Promise<ApiResult<Project>> => {
		const result = parsed(await write('POST', '/projects', req), ProjectSchema);
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
			await write('POST', `/projects/${projectId}/events`, {
				event,
				...(targetNamespace ? { target_namespace: targetNamespace } : {}),
			}),
			ProjectSchema,
		),
);

/**
 * Replace a project's ontology. `can_manage` server-side; 409 once the project is past labeling.
 *
 * WHOLE-DOCUMENT, matching the route: a partial merge would have to answer "what does an absent
 * `classes` mean" — cleared or unchanged — and the two readings differ by an entire taxonomy.
 *
 * Items already sent are unaffected. Each captured its own copy at send, so work in review is
 * judged by the contract it was issued under; that capture is what makes editing during `labeling`
 * safe enough to allow at all.
 */
export const updateProjectOntology = command(
	v.object({ projectId: v.string(), ontology: OntologyWireSchema }),
	async ({ projectId, ontology }): Promise<ApiResult<Project>> => {
		const result = parsed(
			await write('PATCH', `/projects/${projectId}/ontology`, { ontology }),
			ProjectSchema,
		);
		// Single-flight the read this write invalidates, like every other mutation here.
		if (result.ok) void fetchProject({ projectId }).refresh();
		return result;
	},
);

/** Send items into a labeling task — each becomes a claimable item (×`consensus_n` replicas). */
export const sendItems = command(
	v.object({ projectId: v.string(), items: v.array(SendItemSchema) }),
	async ({
		projectId,
		items,
	}): Promise<ApiResult<{ sent: number; created: number; task_ids: string[] }>> =>
		parsed(
			await write('POST', `/projects/${projectId}/items`, { items }),
			v.object({ sent: v.number(), created: v.number(), task_ids: v.array(v.string()) }),
		),
);

/** Consensus v1's merge step: name one accepted replica of a group canonical (a pick, never a
 *  blend). PUT — re-picking while the project is adjudicable is the intended shape. */
export const adjudicate = command(
	v.object({ projectId: v.string(), groupId: v.string(), taskId: v.string() }),
	async ({ projectId, groupId, taskId }): Promise<ApiResult<Project>> =>
		parsed(
			await write('PUT', `/projects/${projectId}/adjudications/${enc(groupId)}`, {
				task_id: taskId,
			}),
			ProjectSchema,
		),
);

/** Withdraw a group's pick. Exists because the publish refuses a stale pick — without removal one
 *  wrong pick would wedge the publish permanently. Body-less, exactly as the DELETE proxy forwarded. */
/**
 * Remove one item from a project.
 *
 * The exit an unfinishable item had none of: an item naming a media dataset that was renamed or
 * removed cannot be opened, so it cannot be claimed, submitted or skipped — and the publish
 * precondition requires every task terminal, so one of them wedges the project permanently.
 *
 * Returns the actor's own report rather than the Project, because removal touches the task INDEX,
 * not the project document, and reporting a project here would invite reading a count off it that
 * this call did not change.
 */
export const dropTask = command(
	v.object({ projectId: v.string(), taskId: v.string() }),
	async ({ projectId, taskId }): Promise<ApiResult<{ task_id: string; removed: boolean }>> => {
		const result = await write('DELETE', `/projects/${projectId}/tasks/${enc(taskId)}`);
		// Single-flight the listing this invalidates — with the arguments the RENDER SITES use.
		//
		// This said `listTasks({ projectId })` and had therefore never refreshed anything. Queries are
		// keyed by their serialized argument, and both surfaces that render this listing ask for
		// `{ projectId, details: true }` (the queue page, and the canvas's label stream) — the bare
		// form is a cache key nothing holds, so the refresh resolved against no instance and returned
		// silently. The queue still appeared to update, because its caller re-loads the page data
		// itself; the single-flight was decoration.
		//
		// The SvelteKit docs name this exact failure: "if `getPosts({ filter: 'author:santa' })` is
		// rendered on the client, calling `getPosts().refresh()` in the server handler won't update
		// it." A refresh that misses is indistinguishable from one that lands until you remove the
		// thing that was covering for it.
		if (result.ok) void listTasks({ projectId, details: true }).refresh();
		return result as ApiResult<{ task_id: string; removed: boolean }>;
	},
);

export const clearAdjudication = command(
	v.object({ projectId: v.string(), groupId: v.string() }),
	async ({ projectId, groupId }): Promise<ApiResult<Project>> =>
		parsed(
			await write('DELETE', `/projects/${projectId}/adjudications/${enc(groupId)}`),
			ProjectSchema,
		),
);
