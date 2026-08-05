import { lineageFeed } from './feeds.remote';

// The MECHANISM (`liveRead`, `LiveCursor`) is `@rask/ui/live-read`, shared with every zone. Only the
// BINDING is per-zone, and it has to be: `query.live` must be declared inside an app to get its own
// endpoint, and `getRequestEvent` only exists there — which is the same reason `feeds.remote.ts` is
// copied into all seven zones rather than shared. So a zone's cost of cursor-driven reads is the few
// lines below, not a copy of the hook.
import type { LiveCursor } from '@rask/ui/live-read';

export { liveRead, type LiveCursor } from '@rask/ui/live-read';

/**
 * The lineage cursor, taken from the ONE shared `lineageFeed` stream — the same feed the shell's
 * notification bell renders. SvelteKit dedupes identical live-query instances onto a single
 * connection, so every lineage-backed surface on a page and the bell above it ride the same stream
 * and cannot disagree about when the estate changed.
 *
 * WHAT IT CARRIES FOR AN INGEST RUN, because the distinction decides how the run view is built: a
 * run emits lineage at START and again at COMPLETE/FAIL, so the cursor moves at BOTH ENDS of a run.
 * That makes it the right signal for "this run reached a terminal state" — which the run page used
 * to learn up to two seconds late, from a timer. It is NOT a progress signal: `units_done` climbs
 * once per fetched unit inside the workflow and moves no cursor at all.
 *
 * Pass it (not a call of it) to `liveRead`, which opens it on mount:
 *
 *     liveRead(lineageTick, () => load());
 */
export function lineageTick(): LiveCursor {
	const feed = lineageFeed();
	return {
		get current() {
			return feed.current?.cursor;
		},
		get connected() {
			return feed.connected;
		},
	};
}
