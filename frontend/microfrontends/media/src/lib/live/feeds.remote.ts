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
