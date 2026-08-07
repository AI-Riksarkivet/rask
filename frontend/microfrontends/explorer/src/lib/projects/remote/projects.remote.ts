import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { projectsJSON, sessionGate, typedAs } from '$lib/server/doors';
import type { CreatedProject, ProjectsList, SendItem, SendResult } from '../projects';

// The SEND half of the annotation funnel, in the zone's remote-function dialect (the transport ruling,
// area 3) — same three operations, same shapes at the call site, transport only. The deleted
// `api/projects/[...path]` route was a passthrough whose actual traffic was exactly these: list the
// projects still taking items, create one, append a selection to it. Naming them removes the
// passthrough (a catch-all forwards whatever a caller spells, including the whole project plane media
// has no business reaching) without adding a single capability.
//
// Dev seam, transport and write gate all live in `$lib/server/doors` over `@rask/api/upstream`
// (#93) — this file is the plane's named operations and nothing else.

/** The send item's arg schema — the boundary check for what the dialog builds (see `SendItem`). */
const SendItemSchema: v.GenericSchema<SendItem> = v.object({
	source: v.object({
		kind: v.literal('chunks'),
		keys: v.array(v.string()),
		where: v.nullable(v.string()),
		dataset_version: v.nullable(v.number()),
	}),
	media: v.object({ kind: v.literal('image') }),
	// DECLARED, because valibot's `v.object` strips what it does not — in silence. Undeclared, a
	// bulk label posts 200, seeds every task, and carries nothing: the send looks like it worked and
	// every queued item is blank. That defect has now been shipped twice in the annotator zone.
	prediction: v.optional(v.array(v.object({ shape_type: v.string(), label: v.string() }))),
});

/** The projects of one tenant. Only draft/labeling ones can take items; that filter stays where it was
 *  (the picker), because it is a rendering rule and the server's list is the server's list. */
export const listProjects = query(
	v.object({ tenant: v.string() }),
	async ({ tenant }): Promise<ApiResult<ProjectsList>> =>
		typedAs<ProjectsList>(await projectsJSON(`/projects?tenant=${encodeURIComponent(tenant)}`)),
);

/** Create a project around a selection. Refreshes the picker in the same flight — this is the one write
 *  that changes WHICH PROJECTS EXIST; a send changes only their counts, which nothing here renders. */
export const createProject = command(
	v.object({ tenant: v.string(), slug: v.string(), title: v.string() }),
	async (draft): Promise<ApiResult<CreatedProject>> => {
		const refused = sessionGate();
		if (refused) return refused;
		const result = typedAs<CreatedProject>(
			await projectsJSON('/projects', { method: 'POST', body: JSON.stringify(draft) }),
		);
		if (result.ok) void listProjects({ tenant: draft.tenant }).refresh();
		return result;
	},
);

/** Append a selection to a project as claimable tasks. The server's refusal is surfaced verbatim: 403
 *  names the missing rung, 409 names a closed project, and neither may read as a silent no-op. */
export const sendProjectItems = command(
	v.object({ projectId: v.string(), items: v.array(SendItemSchema) }),
	async ({ projectId, items }): Promise<ApiResult<SendResult>> => {
		const refused = sessionGate();
		if (refused) return refused;
		return typedAs<SendResult>(
			await projectsJSON(`/projects/${encodeURIComponent(projectId)}/items`, {
				method: 'POST',
				body: JSON.stringify({ items }),
			}),
		);
	},
);
