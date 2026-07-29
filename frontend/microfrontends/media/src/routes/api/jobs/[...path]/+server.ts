import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// Same-origin read proxy to the ANNOTATOR service's batch-jobs domain (status polling).
// GET-only — the submit POST is the dedicated /api/jobs/apply route beside this one.
const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

export const GET = makeBackendProxy({ backendUrl: ANNOTATOR_API, stripPrefix: KEEP_API_PREFIX });
