import { command, getRequestEvent, query } from '$app/server';
import * as v from 'valibot';
import { ACTIVE_PROJECT_COOKIE } from '@rask/api/bff';
import type { ApiResult } from '@rask/api/client';
import {
	RegisteredTasksSchema,
	TransformDeleteSchema,
	TransformDraftSchema,
	TransformListSchema,
	TransformSpecSchema,
	type RegisteredTasks,
	type TransformDelete,
	type TransformList,
	type TransformSpec,
} from '$lib/transforms';
import { catalogJSON, parsed } from '$lib/server/doors';

// Transport: the zone's CATALOG door. A transform declaration is a governed catalog record — stored
// beside table policies and grants, gated on `project:<id>#can_administer`, and landed on the #41
// audit trail — so it rides the same seam every other catalog read/write in this zone does.
//
// WHY REMOTE FUNCTIONS AND NOT `+server.ts`. The estate's transport ruling is one transport per
// payload KIND: typed values ride remote functions, bytes ride `+server.ts`. A transform is six short
// fields in and a small JSON record out — a value on both sides.
//
// WHY THE BEARER IS LOAD-BEARING. Every one of these four doors checks `can_administer` EXPLICITLY
// (`/v1/project` is not a router-guarded resource prefix, and `project` defines no reader-tier
// relation, so even DESCRIBE gates at the admin tier). A remote function runs on the zone server and
// forwards exactly one credential: the signed-in human's bearer. That is what the door accepts.

const enc = encodeURIComponent;

// THE PREFIX IS SPLIT, AND THE SPLIT IS NOT A TYPO. The catalog mounts these four doors on TWO
// routers: `project_router` at `/v1/project` (SINGULAR) carries set/describe/delete, while
// `projects_router` at `/v1/projects` (PLURAL) carries the list. Using the plural for all four is
// the obvious mistake and it FAILS ASYMMETRICALLY — the list answers 200 and only the writes 404,
// so the page looks wired until someone tries to save. Verified live 2026-08-23.

/** The active project, or `''`. Every zone visit happens INSIDE a project (#103) and the transform doors
 *  take it from the gated PATH, so a missing cookie is a REFUSAL rather than an estate-wide read. */
function activeProject(): string {
	return getRequestEvent().cookies.get(ACTIVE_PROJECT_COOKIE) ?? '';
}

/** The 400 a missing active project earns, shaped like any other failed `ApiResult` so a component
 *  branches on `status` alone and never needs a second error channel. */
function noProject<T>(): ApiResult<T> {
	return {
		ok: false,
		status: 400,
		detail: 'No active project — pick one before declaring a transform.',
	};
}

/** Every transform declared in the active project. Admin-gated: a non-admin gets 403, which the page
 *  renders as a denial with its reason rather than as an empty list (#143 — show disabled, never
 *  hide; an empty table would read as "no transforms exist", which is a different and false statement). */
export const listTransforms = query(async (): Promise<ApiResult<TransformList>> => {
	const project = activeProject();
	if (project === '') return noProject<TransformList>();
	return parsed(await catalogJSON(`/v1/projects/${enc(project)}/transforms`), TransformListSchema);
});

/** What may be named as a transform's `task`.
 *
 * The declaration door refuses an unregistered task correctly, and without this the operator learns
 * only that their guess was wrong — a governed field whose vocabulary cannot be read is a trap, not
 * a gate. Estate-wide records answered under the project path, at the same `can_administer` tier as
 * declaring: seeing the choices and making one are the same act. */
export const listRegisteredTasks = query(async (): Promise<ApiResult<RegisteredTasks>> => {
	const project = activeProject();
	if (project === '') return noProject<RegisteredTasks>();
	return parsed(await catalogJSON(`/v1/projects/${enc(project)}/tasks`), RegisteredTasksSchema);
});

/** Declare or replace one transform.
 *
 * `set` is an UPSERT at the door, so this is both create and edit — the catalog keys on
 * (project, transform) and a second set under the same name replaces the record rather than 409ing.
 * Single-flights its own list read so the table re-renders from the server's answer rather than
 * from an assumption about what the write did — `void`, never `await`, so the refresh cannot gate
 * the mutation's own return. */
export const setLane = command(
	TransformDraftSchema,
	async (draft): Promise<ApiResult<TransformSpec>> => {
		const project = activeProject();
		if (project === '') return noProject<TransformSpec>();
		const result = parsed(
			await catalogJSON(`/v1/project/${enc(project)}/transform/set`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(draft),
			}),
			TransformSpecSchema,
		);
		void listTransforms().refresh();
		return result;
	},
);

/** Delete one transform by name.
 *
 * An unknown transform is 422 NAMING THE KEY, not 404 — the URL is right and the key inside it is not,
 * which is malformed in exactly the way a bad enum value is. The page surfaces that distinction
 * rather than flattening both to "gone". Delete itself is idempotent, and `status` is what tells
 * "I removed it" from "it was already gone". */
export const deleteTransform = command(
	v.object({ name: v.pipe(v.string(), v.trim(), v.minLength(1)) }),
	async ({ name }): Promise<ApiResult<TransformDelete>> => {
		const project = activeProject();
		if (project === '') return noProject<TransformDelete>();
		// PARSED AS A DELETE ANSWER, not as a spec. The door replies `{status, project, name}` — none
		// of the record's own fields come back, so validating it as a spec turned every successful
		// delete into a shape drift.
		const result = parsed(
			await catalogJSON(`/v1/project/${enc(project)}/transform/delete`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ name }),
			}),
			TransformDeleteSchema,
		);
		void listTransforms().refresh();
		return result;
	},
);
