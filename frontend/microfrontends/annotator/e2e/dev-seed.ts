/**
 * Dev fixtures for `make dev-zone ZONE=annotator` — see `lakehouse/e2e/dev-seed.ts` for the rationale.
 *
 * DEV data, not test data: no spec imports this. Shapes are copied from `projects.spec.ts` and
 * `data-manager.spec.ts`, plus `@rask/api/runs-feed`'s `LineageProbeSchema` for the cursor.
 *
 * NO BEARER MATTERS HERE. `mock-annotator.ts` keys its seed store GLOBALLY, not per bearer, because this
 * zone's suite runs auth-OFF and every request arrives with no bearer — so there is no identity to key on
 * and the launcher's `MOCK_DEV_BEARER` is simply inert for this zone. That mock never reads an
 * Authorization header at all.
 *
 * THE CURSOR IS SEEDED FIRST, for the reason the models fixture documents at length: surfaces hung off
 * `liveRead` read only when the lineage cursor moves, and this zone's cursor env defaults to a dead
 * `:8001` — so `dev-zone` points `LINEAGE_API` at this mock and seeds the probe.
 *
 * WHAT THIS CANNOT REACH. Some of this zone's specs mock at the BROWSER boundary with `page.route` —
 * identity rides `capi/v1/me`, a BFF pass-through intercepted there rather than seeded. A human's browser
 * has no such interception, so identity-dependent chrome still renders its unauthenticated state under
 * `dev-zone`. Only the SERVER-side reads below are seedable.
 */

/** The cursor probe's contract, exactly as `LineageProbeSchema` parses it: `{events:[{seq:number}]}`. */
export const CURSOR_PROBE = { events: [{ seq: 1 }] };

/** One project, in the shape `GET /projects` returns rows of. Counts are non-zero on purpose — the card
 *  renders "N/M items terminal" from them, and an all-zero project reads as a broken card. */
const PROJECT = {
	project_id: 'p1',
	tenant: 'default',
	slug: 'vasa-portraits',
	title: 'Vasa portraits',
	description: 'Portrait shapes on the Vasa corpus',
	state: 'labeling',
	review_required: true,
	lease_seconds: 1800,
	counts: {
		unassigned: 1,
		claimed: 0,
		in_review: 1,
		changes_requested: 0,
		accepted: 1,
		skipped: 0,
	},
	published: null,
	publish_error: null,
	publish_progress: null,
	pending_target_namespace: null,
};

/** A task row as the queue listing carries it — one per state worth seeing in the manager. */
const task = (task_id: string, state: string) => ({
	task_id,
	project_id: 'p1',
	state,
	assignee: null,
	lease_expires_at: null,
	source: { kind: 'chunks', keys: ['dev-key-1'] },
	media: { kind: 'image', image_url: null },
	review_required: true,
});

export const ANNOTATOR_SEED: Record<string, unknown> = {
	// LIVENESS FIRST — see the header.
	'GET /events?limit=1&summary=true': CURSOR_PROBE,
	'GET /runs': { runs: [] },

	// `listProjects` calls `/projects?tenant=<t>`; the mock tries the with-query key first and then falls
	// back to path-only, so this one entry covers every tenant a dev switches to.
	'GET /projects': { projects: [PROJECT], total: 1 },
	'GET /projects/p1': { project: PROJECT, legal_events: [] },
	'GET /projects/p1/tasks?include=details': {
		tasks: [task('t1', 'unassigned'), task('t2', 'in_review'), task('t3', 'accepted')],
		total: 3,
	},
	'GET /projects/p1/members': { members: [], total: 0 },
	'GET /api/assist/producers': { producers: [] },
};

export const DEV_SEEDS: {
	env: string;
	path?: string;
	body?: unknown;
	routes?: Record<string, unknown>;
}[] = [{ env: 'ANNOTATOR_API', routes: ANNOTATOR_SEED }];
