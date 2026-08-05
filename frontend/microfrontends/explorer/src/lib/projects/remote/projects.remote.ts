import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { CreatedProject, ProjectsList, SendItem, SendResult } from '../projects';

// The SEND half of the annotation funnel, in the zone's remote-function dialect (the transport ruling,
// area 3) — same three operations, same shapes at the call site, transport only. The deleted
// `api/projects/[...path]` route was a passthrough whose actual traffic was exactly these: list the
// projects still taking items, create one, append a selection to it. Naming them removes the
// passthrough (a catch-all forwards whatever a caller spells, including the whole project plane media
// has no business reaching) without adding a single capability.
//
// Dev seam, unchanged: the projects/task plane may live on a DIFFERENT annotator than the media plane
// (cluster actors + a locally-seeded corpus), so `ANNOTATOR_PROJECTS_API` wins when it is set and
// falls back to `ANNOTATOR_API` — one service in any real deploy.
const ANNOTATOR_API = env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** The write gate the deleted POST route carried (`requireSession: true`): on an auth-enabled stack a
 *  send must be attributable — it seeds task provenance — so an anonymous caller is refused before the
 *  request leaves the zone. The GET had no such gate and still has none. */
function sessionGate(): { ok: false; status: number; detail: string } | null {
	const { locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in required' };
	}
	return null;
}

async function projectsJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
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
		// UNREACHABLE is status 0, distinct from any answer the service gave: the dialog prints the
		// detail verbatim, and "could not reach the annotator" is a different sentence from "the
		// annotator refused you".
		return { ok: false, status: 0, detail: String(err) };
	}
	if (!res.ok) {
		let detail = `the annotator answered ${res.status}`;
		try {
			const body: unknown = await res.json();
			if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
		} catch {
			/* a non-JSON error body keeps the status-line detail */
		}
		return { ok: false, status: res.status, detail };
	}
	return { ok: true, data: await res.json() };
}

/** The projects plane's own shapes are the contract (the deleted route parsed nothing and neither did
 *  the dialog); this hands the payload on with the type the call site already used. */
function typedAs<T>(result: ApiResult<unknown>): ApiResult<T> {
	return result.ok ? { ok: true, data: result.data as T } : result;
}

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
