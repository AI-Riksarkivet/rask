import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// Same-origin proxy to the ANNOTATOR service's projects plane — the SEND half of the funnel:
// media is where data points are FOUND (search hits, an atlas lasso), and a selection is sent
// (appended) into an annotation project, or a new project is created around it, from HERE.
// Bare `/projects` upstream, so the `/api` prefix is stripped; writes are session-gated on an
// auth-enabled stack (a send must be attributable — it seeds task provenance).
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
