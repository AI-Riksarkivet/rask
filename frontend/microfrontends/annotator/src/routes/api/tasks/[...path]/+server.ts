import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// The task half of the projects plane (bare `/tasks/...` upstream): task reads, task events
// (claim/submit/release/skip/accept/fix_and_accept/request_changes/…) and the draft read/save.
// PUT is the draft save — revision-guarded upstream (409 on a stale `base_revision`), so the
// proxy forwards status codes untouched and the client surfaces the conflict honestly.
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

export const PUT = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: /^\/api/,
	requireSession: true,
});
