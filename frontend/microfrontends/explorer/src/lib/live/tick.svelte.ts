import { liveRead, type LiveCursor } from '@rask/api/live';
import { lineageFeed } from './feeds.remote';

/**
 * This zone's live CURSOR — the explorer's half of #73.
 *
 * The reader itself (`liveRead`, and the four rules that make it not a timer) lives in
 * `@rask/api/live`. What stays here is the half that CANNOT move: a cursor wraps THIS app's own
 * `feeds.remote.ts`, because `query.live` is declared per app so it gets its own endpoint and can
 * reach `getRequestEvent`. Same seam `@rask/api/runs-feed` uses — share the body, keep the
 * app-bound shim in the app.
 *
 * WHY THE EXPLORER NEEDED ONE. It already had `lineageFeed`, but the only thing reading it was the
 * shell's notification bell: the zone could TELL you the estate had changed while every surface
 * under the notification went on showing the state from before it. Its Arrow reads — the atlas
 * points and the search results — were read once at mount and never again, so a mover run, a
 * compaction or anyone else's write left a corpus view that was quietly out of date. Stale search
 * results are worse than a stale table, because the next thing someone does with them is SEND them
 * to a labelling project.
 *
 * Re-exported so this zone's consumers import liveness from one place — a surface asks its ZONE for
 * it rather than reaching past the zone into a package.
 */
export { liveRead, type LiveCursor };

/**
 * The lineage cursor, from the ONE shared `lineageFeed` stream — the same feed the bell renders.
 * SvelteKit dedupes identical live-query instances onto a single connection, so every lineage-backed
 * surface on the page and the bell above it ride one stream, advance on the same number, and cannot
 * disagree about when the estate changed.
 *
 * Pass it (not a call of it) to `liveRead`, which opens it on mount:
 *
 *     liveRead(lineageTick, () => reload());
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
