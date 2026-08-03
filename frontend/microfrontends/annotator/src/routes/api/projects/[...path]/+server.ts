import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// Same-origin proxy to the ANNOTATOR service's projects plane (bare `/projects` upstream — unlike
// the media annotations routes, these do NOT live under `/api` on the service, so the `/api` prefix
// is STRIPPED, not kept). GET serves the landing/detail/queue reads; POST drives create, project
// events (open/freeze/publish/archive), sends and task events — every one FGA-gated server-side,
// so the proxy's job is bearer-forwarding, and `requireSession` keeps writes attributable on an
// auth-enabled stack (the confused-deputy stance shared with the annotations save route).
// Dev seam: the projects/task plane may live on a DIFFERENT annotator than the media plane
// (e.g. cluster actors + a locally-seeded corpus). Falls back to ANNOTATOR_API — one service in
// any real deploy.
const ANNOTATOR_API = env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';

export const GET = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: /^\/api/,
});

export const POST = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: /^\/api/,
	requireSession: true,
});

// Adjudication (consensus v1's merge step): PUT picks, DELETE withdraws. Found missing by the
// LIVE drive — the hermetic e2e mocks at the browser boundary, so a verb the proxy never
// exports still passes there while the real BFF answers 405.
export const PUT = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: /^\/api/,
	requireSession: true,
});

export const DELETE = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: /^\/api/,
	requireSession: true,
});
