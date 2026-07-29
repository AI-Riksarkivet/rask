import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// Explicit POST route: interactive AI-assist over one unit (GroundingDINO text / SAM
// region → predicted shapes). Enumerated write-plane surface — bearer-forwarding,
// fail-closed without a session on an auth-enabled stack.
const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

export const POST = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: KEEP_API_PREFIX,
	requireSession: true,
});
