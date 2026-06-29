import { query, getRequestEvent } from '$app/server';
import { listProjects, type Project } from '@rask/api';

// THE ONE PATTERN (rask-frontend canon §1): a server-only query() whose body
// calls a @rask/api function with getRequestEvent().fetch, so SSR resolves the
// relative /api/* against the request (rewritten to the gateway in hooks.server.ts),
// reuses @rask/api's valibot parse, and inlines the result into the SSR payload.
export const getProjects = query(async (): Promise<Project[]> => {
	const { projects } = await listProjects(getRequestEvent().fetch);
	return projects;
});
