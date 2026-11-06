import { command, query } from '#lib/reads.svelte.ts';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';

// THE PROJECTS VIEW PREFERENCE — gallery or table — per person.
//
// No door on this stack keeps per-person state: the lakehouse keeps none, and the console has no
// backend of its own. So the choice holds for the page it was made on, the read answers "never
// chosen" and the write answers the refusal, both without a request. The toggle stays, because the
// view itself works; only remembering it does not.

/** The two views the projects list offers. */
const ViewSchema = v.picklist(['gallery', 'table']);
export type ProjectsView = v.InferOutput<typeof ViewSchema>;

/**
 * The caller's saved projects view, or null when they have never chosen one.
 *
 * NULL IS NOT AN ERROR and is not "gallery": the caller decides the default, and conflating "no
 * preference" with "gallery" would mean a default change silently rewrites what people who never
 * chose appear to have chosen. With nowhere to keep a choice, every read answers null.
 */
export const readProjectsView = query(async (): Promise<ProjectsView | null> => null);

/** Save the caller's choice: refused, naming what is missing, so the caller can say it was not kept. */
export const writeProjectsView = command(ViewSchema, async (): Promise<ApiResult<unknown>> => ({
	ok: false,
	status: 501,
	detail: 'no door on this stack keeps per-person state: the projects view is not saved',
}));
