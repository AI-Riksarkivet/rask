/**
 * The typed BFF client for the projects plane — same-origin `/api/projects` + `/api/tasks`
 * (this zone's dialect: `+server.ts` proxy + `createBffClient`, never remote `query()`).
 *
 * Every response is valibot-parsed at this boundary, so a contract drift is a named decode error
 * here rather than an `undefined` three components deep. Writes return the `ApiResult` split
 * untouched — 401 ≠ 403 ≠ 409 matter to the UI (sign in, no permission, stale revision), and
 * collapsing them would turn three different next-steps into one dead toast.
 */
import { createBffClient, type ApiResult } from '@rask/api/client';
import { base } from '$app/paths';
import * as v from 'valibot';

import {
	DraftSchema,
	ProjectDetailSchema,
	ProjectListSchema,
	ProjectSchema,
	TaskDetailSchema,
	TaskListingSchema,
	type Draft,
	type Project,
	type ProjectDetail,
	type ProjectList,
	type TaskDetail,
	type TaskListing,
} from './types.js';

const bff = createBffClient(base);

/** JSON write with the content-type set — a browser `fetch` with a bare string body defaults to
 *  `text/plain`, the BFF forwards it verbatim, and FastAPI answers 422 (found by the live drive). */
const writeJSON = (method: 'POST' | 'PUT', payload: unknown): RequestInit => ({
	method,
	headers: { 'content-type': 'application/json' },
	body: JSON.stringify(payload),
});

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

export type SendItem = {
	source: { kind: string; keys: string[]; where?: string | null };
	media: { kind: string; image_url?: string | null; media_url?: string | null };
};

export const projectsApi = {
	list: async (tenant: string): Promise<ApiResult<ProjectList>> =>
		parsed(
			ProjectListSchema,
			await bff.requestJSON('/api', `projects?tenant=${encodeURIComponent(tenant)}`),
		),

	get: async (projectId: string): Promise<ApiResult<ProjectDetail>> =>
		parsed(ProjectDetailSchema, await bff.requestJSON('/api', `projects/${projectId}`)),

	create: async (req: {
		tenant: string;
		slug: string;
		title?: string;
		description?: string;
		instructions?: string;
		review_required?: boolean;
		lease_seconds?: number;
		consensus_n?: number;
		label_schema?: { classes: { name: string; shape_types?: string[] }[]; attributes?: string[] };
	}): Promise<ApiResult<Project>> =>
		parsed(ProjectSchema, await bff.requestJSON('/api', 'projects', writeJSON('POST', req))),

	fireEvent: async (
		projectId: string,
		event: string,
		targetNamespace?: string,
	): Promise<ApiResult<Project>> =>
		parsed(
			ProjectSchema,
			await bff.requestJSON(
				'/api',
				`projects/${projectId}/events`,
				writeJSON('POST', {
					event,
					...(targetNamespace ? { target_namespace: targetNamespace } : {}),
				}),
			),
		),

	sendItems: async (
		projectId: string,
		items: SendItem[],
	): Promise<ApiResult<{ sent: number; created: number; task_ids: string[] }>> =>
		parsed(
			v.object({ sent: v.number(), created: v.number(), task_ids: v.array(v.string()) }),
			await bff.requestJSON('/api', `projects/${projectId}/items`, writeJSON('POST', { items })),
		),

	/** Consensus v1's merge step: name one accepted replica of a group canonical (a pick, never a
	 *  blend). PUT — re-picking while the project is adjudicable is the intended shape. */
	adjudicate: async (
		projectId: string,
		groupId: string,
		taskId: string,
	): Promise<ApiResult<Project>> =>
		parsed(
			ProjectSchema,
			await bff.requestJSON(
				'/api',
				`projects/${projectId}/adjudications/${encodeURIComponent(groupId)}`,
				writeJSON('PUT', { task_id: taskId }),
			),
		),

	/** Withdraw a group's pick. Exists because the publish refuses a stale pick — without removal
	 *  one wrong pick would wedge the publish permanently. */
	clearAdjudication: async (projectId: string, groupId: string): Promise<ApiResult<Project>> =>
		parsed(
			ProjectSchema,
			await bff.requestJSON(
				'/api',
				`projects/${projectId}/adjudications/${encodeURIComponent(groupId)}`,
				{ method: 'DELETE' },
			),
		),

	listTasks: async (
		projectId: string,
		opts?: { details?: boolean },
	): Promise<ApiResult<TaskListing>> =>
		parsed(
			TaskListingSchema,
			await bff.requestJSON(
				'/api',
				`projects/${projectId}/tasks${opts?.details ? '?include=details' : ''}`,
			),
		),
};

export const tasksApi = {
	get: async (taskId: string): Promise<ApiResult<TaskDetail>> =>
		parsed(TaskDetailSchema, await bff.requestJSON('/api', `tasks/${taskId}`)),

	fireEvent: async (
		taskId: string,
		event: string,
		opts?: { assignee?: string; message?: string; shape_ids?: string[]; lease_seconds?: number },
	): Promise<ApiResult<TaskDetail>> =>
		parsed(
			TaskDetailSchema,
			await bff.requestJSON(
				'/api',
				`tasks/${taskId}/events`,
				writeJSON('POST', { event, ...opts }),
			),
		),

	getDraft: async (taskId: string): Promise<ApiResult<Draft>> =>
		parsed(DraftSchema, await bff.requestJSON('/api', `tasks/${taskId}/draft`)),

	saveDraft: async (
		taskId: string,
		req: { shapes: unknown[]; base_revision?: number | null; origin?: string },
	): Promise<ApiResult<Draft>> =>
		parsed(
			DraftSchema,
			await bff.requestJSON('/api', `tasks/${taskId}/draft`, writeJSON('PUT', req)),
		),
};
