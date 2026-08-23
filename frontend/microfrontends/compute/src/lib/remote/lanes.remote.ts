import { command, getRequestEvent, query } from '$app/server';
import * as v from 'valibot';
import { ACTIVE_PROJECT_COOKIE } from '@rask/api/bff';
import type { ApiResult } from '@rask/api/client';
import {
	LaneDraftSchema,
	LaneListSchema,
	LaneSpecSchema,
	type LaneList,
	type LaneSpec,
} from '$lib/lanes';
import { catalogJSON, parsed } from '$lib/server/doors';

// Transport: the zone's CATALOG door. A lane declaration is a governed catalog record — stored
// beside table policies and grants, gated on `project:<id>#can_administer`, and landed on the #41
// audit trail — so it rides the same seam every other catalog read/write in this zone does.
//
// WHY REMOTE FUNCTIONS AND NOT `+server.ts`. The estate's transport ruling is one transport per
// payload KIND: typed values ride remote functions, bytes ride `+server.ts`. A lane is six short
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

/** The active project, or `''`. Every zone visit happens INSIDE a project (#103) and the lane doors
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
		detail: 'No active project — pick one before declaring a lane.',
	};
}

/** Every lane declared in the active project. Admin-gated: a non-admin gets 403, which the page
 *  renders as a denial with its reason rather than as an empty list (#143 — show disabled, never
 *  hide; an empty table would read as "no lanes exist", which is a different and false statement). */
export const listLanes = query(async (): Promise<ApiResult<LaneList>> => {
	const project = activeProject();
	if (project === '') return noProject<LaneList>();
	return parsed(await catalogJSON(`/v1/projects/${enc(project)}/transforms`), LaneListSchema);
});

/** Declare or replace one lane.
 *
 * `set` is an UPSERT at the door, so this is both create and edit — the catalog keys on
 * (project, lane) and a second set under the same name replaces the record rather than 409ing.
 * Single-flights its own list read so the table re-renders from the server's answer rather than
 * from an assumption about what the write did — `void`, never `await`, so the refresh cannot gate
 * the mutation's own return. */
export const setLane = command(LaneDraftSchema, async (draft): Promise<ApiResult<LaneSpec>> => {
	const project = activeProject();
	if (project === '') return noProject<LaneSpec>();
	const result = parsed(
		await catalogJSON(`/v1/project/${enc(project)}/transform/set`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(draft),
		}),
		LaneSpecSchema,
	);
	void listLanes().refresh();
	return result;
});

/** Delete one lane by name.
 *
 * An unknown lane is 422 NAMING THE KEY, not 404 — the URL is right and the key inside it is not,
 * which is malformed in exactly the way a bad enum value is. The page surfaces that distinction
 * rather than flattening both to "gone". */
export const deleteLane = command(
	v.object({ lane: v.pipe(v.string(), v.trim(), v.minLength(1)) }),
	async ({ lane }): Promise<ApiResult<LaneSpec>> => {
		const project = activeProject();
		if (project === '') return noProject<LaneSpec>();
		const result = parsed(
			await catalogJSON(`/v1/project/${enc(project)}/transform/delete`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ lane }),
			}),
			LaneSpecSchema,
		);
		void listLanes().refresh();
		return result;
	},
);
