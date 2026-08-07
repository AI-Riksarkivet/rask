import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	SIGN_IN_REQUIRED,
	parsed,
	projectsJSON as annotatorJSON,
	signedOut,
} from '$lib/server/doors';
import { DraftSchema, TaskDetailSchema, type Draft, type TaskDetail } from '../types.js';
import { listTasks } from './projects.remote';

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

const write = (method: 'POST' | 'PUT', path: string, body: unknown): Promise<ApiResult<unknown>> =>
	signedOut()
		? Promise.resolve(SIGN_IN_REQUIRED)
		: annotatorJSON(path, { method, body: JSON.stringify(body) });

const TaskIdArg = v.object({ taskId: v.string() });

/** One task document with its own legal events. */
export const fetchTask = query(
	TaskIdArg,
	async ({ taskId }): Promise<ApiResult<TaskDetail>> =>
		parsed(await annotatorJSON(`/tasks/${taskId}`), TaskDetailSchema),
);

/** The task draft as the publish will read it. A 404 here means "not written yet" and MUST stay
 *  distinguishable from a failure — `draft-sync.ts` omits `base_revision` on 404 and aborts on
 *  anything else. */
export const fetchDraft = query(
	TaskIdArg,
	async ({ taskId }): Promise<ApiResult<Draft>> =>
		parsed(await annotatorJSON(`/tasks/${taskId}/draft`), DraftSchema),
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
		/** The project whose LISTING this transition invalidates — refresh-only, never sent upstream.
		 *
		 *  A task event changes what the queue shows (a claim moves a row out of `unassigned`, a
		 *  submit moves it to `in_review`), but the endpoint is addressed by task alone, so this
		 *  command could not name the query to refresh and refreshed nothing. Its callers compensated
		 *  by re-reading the whole page afterwards — the two round-trips single-flight mutations exist
		 *  to collapse into one.
		 *
		 *  Optional because a caller that has no project context (the canvas saving a draft) must
		 *  still be able to fire an event; it simply gets no listing refresh, which is correct — there
		 *  is no listing on screen to update. */
		projectId: v.optional(v.string()),
	}),
	async ({ taskId, projectId, ...body }): Promise<ApiResult<TaskDetail>> => {
		const result = parsed(await write('POST', `/tasks/${taskId}/events`, body), TaskDetailSchema);
		// `{ projectId, details: true }` — the arguments the render sites use. The bare form is a
		// different cache key and would refresh nothing (the defect `dropTask` carried).
		//
		// One refresh serves BOTH surfaces, because both hold that same key: the queue page and the
		// canvas's label stream. So claiming an item from the queue also corrects the canvas's
		// prev/next set, with no second request and no poll.
		if (result.ok && projectId) void listTasks({ projectId, details: true }).refresh();
		return result;
	},
);

/** Snapshot the canvas's shapes into the task draft (the S10 canvas→draft sync). Revision-guarded
 *  upstream; on success the draft query is refreshed in the same flight so the NEXT sync guards
 *  against the revision this save produced rather than the one it started from — a cached read
 *  there would 409 every save after the first. */
export const saveDraft = command(
	v.object({
		taskId: v.string(),
		shapes: v.array(v.record(v.string(), v.unknown())),
		/** Typed edges between those shapes. The service drops an unlisted key SILENTLY (`save_draft`
		 *  builds the model field by field so a caller cannot set `revision`), so a schema that omits
		 *  this loses every relation without erroring. */
		links: v.optional(
			v.array(v.object({ name: v.string(), from_shape: v.string(), to_shape: v.string() })),
			[],
		),
		base_revision: v.optional(v.nullable(v.number())),
		origin: v.optional(v.string()),
	}),
	async ({ taskId, ...body }): Promise<ApiResult<Draft>> => {
		const result = parsed(await write('PUT', `/tasks/${taskId}/draft`, body), DraftSchema);
		if (result.ok) void fetchDraft({ taskId }).refresh();
		return result;
	},
);
