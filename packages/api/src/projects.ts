// @rask/api/projects — read-only list of operator Project resources for the home picker.

import * as v from 'valibot';
import { parse } from './parse.js';

export const ProjectSchema = v.object({
	slug: v.string(),
	name: v.string(),
	team: v.string(),
	workload: v.string(),
	phase: v.string(),
	namespace: v.string(),
	url: v.string(),
	created_at: v.string(),
});
export type Project = v.InferOutput<typeof ProjectSchema>;

export const ProjectsPayloadSchema = v.object({ projects: v.array(ProjectSchema) });
export type ProjectsPayload = v.InferOutput<typeof ProjectsPayloadSchema>;

export async function listProjects(fetchFn: typeof fetch = fetch): Promise<ProjectsPayload> {
	const res = await fetchFn('/api/projects/');
	if (!res.ok) throw new Error(`listProjects: HTTP ${res.status}`);
	return parse(ProjectsPayloadSchema, await res.json());
}
