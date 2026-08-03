import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';

// THE PROJECTS VIEW PREFERENCE — gallery or table — persisted per user.
//
// It rides the catalog's `dock-layout` user-state document, which is the estate's ONE per-subject
// store (`@rask/api/dock-layout` documents its three-outcome contract). That document is a map of
// `workbenches`, namespaced by id so surfaces cannot collide, so this preference is simply another
// entry under its own key. A second user-state document for one enum would be a second thing to
// provision, migrate and fail — the map exists precisely so it does not have to be.
//
// Transport is the estate's remote-function dialect, mirroring media's `readUserStateDoc` /
// `writeUserStateDoc` verbatim in shape: the same names for the same job, so the two zones' user
// state cannot drift apart. A BFF route was the OLD answer and was deleted estate-wide.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

/** The key this preference occupies inside the shared document's `workbenches` map. */
const VIEW_KEY = 'projects-view';

/** The two views the projects list offers. Parsed rather than trusted: this value comes back from a
 *  store the user's own browser wrote to, and an unknown string must not reach the UI as a view. */
const ViewSchema = v.picklist(['gallery', 'table']);
export type ProjectsView = v.InferOutput<typeof ViewSchema>;

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** One catalog call → `ApiResult<unknown>`. An unreachable catalog is `{ok:false, status:0}`: a
 *  rejected fetch must never cross the remote boundary. */
async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	let res: Response;
	try {
		res = await fetch(`${CATALOG_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	if (!res.ok) {
		let detail = `catalog answered ${res.status}`;
		try {
			const body: unknown = await res.json();
			if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
		} catch {
			/* a non-JSON error body is not worth a second failure mode */
		}
		return { ok: false, status: res.status, detail };
	}
	try {
		return { ok: true, data: await res.json() };
	} catch (err) {
		return { ok: false, status: 502, detail: `catalog returned a non-JSON body: ${String(err)}` };
	}
}

/** The envelope the catalog serves user-state in: `exists` plus the document itself. */
const EnvelopeSchema = v.object({
	exists: v.optional(v.boolean()),
	value: v.optional(
		v.object({
			workbenches: v.optional(v.record(v.string(), v.unknown())),
		}),
	),
});

/**
 * The caller's saved projects view, or null when they have never chosen one.
 *
 * NULL IS NOT AN ERROR and is not "gallery": the caller decides the default, and conflating "no
 * preference" with "gallery" would mean a future default change silently rewrites what people who
 * never chose appear to have chosen. Every failure — no session, catalog down, drifted document —
 * also answers null, because a preference that cannot be read must degrade to the default view
 * rather than to a broken page. There is nothing here worth an error state.
 */
export const readProjectsView = query(async (): Promise<ProjectsView | null> => {
	const result = await catalogJSON('/v1/user-state/dock-layout');
	if (!result.ok) return null;
	const parsed = v.safeParse(EnvelopeSchema, result.data);
	if (!parsed.success) return null;
	const view = parsed.output.value?.workbenches?.[VIEW_KEY];
	const asView = v.safeParse(ViewSchema, view);
	return asView.success ? asView.output : null;
});

/**
 * Save the caller's choice.
 *
 * READ-MODIFY-WRITE, and it must be: the document is shared with every dock layout in the estate, so
 * PUTting `{workbenches: {[VIEW_KEY]: view}}` would delete every saved workbench the user owns. The
 * read is a fresh one rather than a cached query, because what matters is what is in the document
 * NOW, not what this tab saw when it mounted.
 *
 * Answers `ApiResult` rather than swallowing failure: the toggle is optimistic, but a refused write
 * is a real outcome and the caller decides whether to say so.
 */
export const writeProjectsView = command(
	ViewSchema,
	async (view): Promise<ApiResult<unknown>> => {
		const current = await catalogJSON('/v1/user-state/dock-layout');
		const existing = current.ok ? v.safeParse(EnvelopeSchema, current.data) : null;
		const workbenches = { ...(existing?.success ? (existing.output.value?.workbenches ?? {}) : {}) };
		workbenches[VIEW_KEY] = view;
		return catalogJSON('/v1/user-state/dock-layout', {
			method: 'PUT',
			body: JSON.stringify({ workbenches }),
		});
	},
);
