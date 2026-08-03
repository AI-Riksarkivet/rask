import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { DraftSchema, TaskDetailSchema, type Draft, type TaskDetail } from '../types.js';

// The TASK half of the projects plane (the transport ruling, area 4) — task reads, task events
// (claim / assign / submit / release / skip / accept / fix_and_accept / request_changes / …) and the
// revision-guarded draft read+save. The `/api/tasks/[...path]` proxy is deleted; its three verbs are
// the named functions below, same `ApiResult` shapes at every call site, transport only.
//
// TWO fidelity requirements the proxy carried and this keeps, because `draft-sync.ts` reads BOTH:
//  · the draft read's 404 stays a 404 — "no draft yet" (omit `base_revision`) is a different fact
//    from "the read failed" (abort rather than clobber);
//  · the draft save's 409 stays a 409 — a stale `base_revision` is refused UPSTREAM, and the
//    conflict is surfaced verbatim rather than being retried into an overwrite.
//
// Template enforcement (`enforce: true`) lives in the service's submit transition and is untouched
// here: a refused submit arrives as its 409/422 with the service's own words, which the queue prints.
//
// Session, parse and offline semantics are identical to `projects.remote.ts` — see its header.

const ANNOTATOR_API = env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** The deleted route's `requireSession: true` — a task event or a draft save is never anonymous on
 *  an auth-enabled stack, and the caller sees the same 401 the BFF answered. */
function signedOut(): boolean {
	const { locals } = getRequestEvent();
	return locals.authEnabled && !locals.session;
}

const SIGN_IN_REQUIRED: ApiResult<never> = { ok: false, status: 401, detail: 'sign in required' };

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

const write = (method: 'POST' | 'PUT', path: string, body: unknown): Promise<ApiResult<unknown>> =>
	signedOut()
		? Promise.resolve(SIGN_IN_REQUIRED)
		: annotatorJSON(path, { method, body: JSON.stringify(body) });

const TaskIdArg = v.object({ taskId: v.string() });

/** One task document with its own legal events. */
export const fetchTask = query(
	TaskIdArg,
	async ({ taskId }): Promise<ApiResult<TaskDetail>> =>
		parsed(TaskDetailSchema, await annotatorJSON(`/tasks/${taskId}`)),
);

/** The task draft as the publish will read it. A 404 here means "not written yet" and MUST stay
 *  distinguishable from a failure — `draft-sync.ts` omits `base_revision` on 404 and aborts on
 *  anything else. */
export const fetchDraft = query(
	TaskIdArg,
	async ({ taskId }): Promise<ApiResult<Draft>> =>
		parsed(DraftSchema, await annotatorJSON(`/tasks/${taskId}/draft`)),
);

/** Fire a task transition. Every optional field rides only when the caller set it: `assignee` is the
 *  manager's distribution edge, `message` the reviewer's note, `shape_ids` the shapes a note points
 *  at, `lease_seconds` an explicit hold. */
export const fireTaskEvent = command(
	v.object({
		taskId: v.string(),
		event: v.string(),
		assignee: v.optional(v.string()),
		message: v.optional(v.string()),
		shape_ids: v.optional(v.array(v.string())),
		lease_seconds: v.optional(v.number()),
	}),
	async ({ taskId, ...body }): Promise<ApiResult<TaskDetail>> =>
		parsed(TaskDetailSchema, await write('POST', `/tasks/${taskId}/events`, body)),
);

/** Snapshot the canvas's shapes into the task draft (the S10 canvas→draft sync). Revision-guarded
 *  upstream; on success the draft query is refreshed in the same flight so the NEXT sync guards
 *  against the revision this save produced rather than the one it started from — a cached read
 *  there would 409 every save after the first. */
export const saveDraft = command(
	v.object({
		taskId: v.string(),
		shapes: v.array(v.record(v.string(), v.unknown())),
		base_revision: v.optional(v.nullable(v.number())),
		origin: v.optional(v.string()),
	}),
	async ({ taskId, ...body }): Promise<ApiResult<Draft>> => {
		const result = parsed(DraftSchema, await write('PUT', `/tasks/${taskId}/draft`, body));
		if (result.ok) void fetchDraft({ taskId }).refresh();
		return result;
	},
);
