import { command, getRequestEvent, query } from '$app/server';
import * as v from 'valibot';
import { ACTIVE_PROJECT_COOKIE } from '@rask/api/bff';
import type { ApiResult } from '@rask/api/client';
import {
	GateDescribeSchema,
	GateDraftSchema,
	GateRemovedSchema,
	GateSpecSchema,
	type GateDescribe,
	type GateSpec,
} from '$lib/gates';
import { catalogJSON, parsed } from '$lib/server/doors';

// Transport: the zone's CATALOG door, like the lane surface beside it. A gate declaration is a
// governed catalog record — admin-gated on `project:<id>#can_administer` and landed on the audit
// trail — so it rides the same seam.
//
// THE PREFIX IS SINGULAR. These three doors are on `project_router` (`/v1/project`), not the plural
// `/v1/projects` that carries the transform LIST. Using the plural fails asymmetrically — the same
// mistake cost three turns on the lane surface — so it is stated here rather than rediscovered.

const enc = encodeURIComponent;

function activeProject(): string {
	return getRequestEvent().cookies.get(ACTIVE_PROJECT_COOKIE) ?? '';
}

function noProject<T>(): ApiResult<T> {
	return {
		ok: false,
		status: 400,
		detail: 'No active project — pick one before configuring a gate.',
	};
}

/** This project's declared gate, or `null` when the chart's settings still govern. */
export const getGate = query(async (): Promise<ApiResult<GateDescribe>> => {
	const project = activeProject();
	if (project === '') return noProject<GateDescribe>();
	return parsed(
		await catalogJSON(`/v1/project/${enc(project)}/gate/describe`, { method: 'POST' }),
		GateDescribeSchema,
	);
});

/** Declare or replace this project's gate. Upsert, so this is both create and edit. */
export const setGate = command(GateDraftSchema, async (draft): Promise<ApiResult<GateSpec>> => {
	const project = activeProject();
	if (project === '') return noProject<GateSpec>();
	const result = parsed(
		await catalogJSON(`/v1/project/${enc(project)}/gate/set`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(draft),
		}),
		GateSpecSchema,
	);
	void getGate().refresh();
	return result;
});

/** Stop overriding — the chart's settings govern again.
 *
 * A DELETE, never a write of zeros: a band of 0.0 is a real setting that holds every non-empty
 * delta, so "stop overriding" and "be maximally strict" must not be the same button. */
export const clearGate = command(
	v.object({}),
	async (): Promise<ApiResult<{ removed: boolean }>> => {
		const project = activeProject();
		if (project === '') return noProject<{ removed: boolean }>();
		const result = parsed(
			await catalogJSON(`/v1/project/${enc(project)}/gate/delete`, { method: 'POST' }),
			GateRemovedSchema,
		);
		void getGate().refresh();
		return result;
	},
);
