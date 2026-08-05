import { getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import { lineageAuthHeaders, lineagePulse, type LineagePulse } from '@rask/api/runs-feed';

export type { LineagePulse, RunNotice } from '@rask/api/runs-feed';

/**
 * This zone's run-notification feed.
 *
 * The whole body is `@rask/api/runs-feed`, shared with every other zone — probe the lineage cursor,
 * re-read `/runs` when it moves, failures first, trim to the window, keep the stream alive. What cannot be
 * shared is exactly this file: `query.live` must be declared inside an app to get its own endpoint, and
 * `getRequestEvent` only exists there. So a zone's cost of having the bell is four lines, which is why it
 * had no business shipping in one zone out of four.
 *
 * The credential is THIS REQUEST'S SESSION first, falling back to the read-only service identity, and
 * to nothing at all on an auth-off stack — `lineageAuthHeaders` already degrades that way, and the
 * generator fails quiet on an absent lineage service (the bell just stays empty; no boundary error,
 * no crash).
 *
 * The session half is new. While this zone was `train` it was auth-free — no session existed in its
 * locals, so the service identity was the only credential it could offer. It now serves the model
 * registry, which reads the catalog as the SIGNED-IN user, so the zone gained a session handle
 * (`hooks.server.ts`). Keeping the bell on the service identity after that would have made one page
 * read the estate as two different principals: the lineage feed is governed PER SUBJECT (an event is
 * returned only if the caller `can_get_metadata` on every dataset it references), so the bell would
 * surface run notices for datasets the signed-in user is not permitted to know exist.
 */
const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';

function lineageHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	return lineageAuthHeaders({
		accessToken: locals.session?.accessToken,
		serviceToken: env.LINEAGE_SERVICE_TOKEN,
		serviceId: env.LINEAGE_SERVICE_ID,
	});
}

export const lineageFeed = query.live(function (): AsyncGenerator<LineagePulse> {
	const { fetch } = getRequestEvent();
	return lineagePulse({ lineageApi: LINEAGE_API, fetch, headers: lineageHeaders });
});
