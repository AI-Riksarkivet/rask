import { liveRead, type LiveCursor } from '@rask/api/live';
import { lineageFeed } from './feeds.remote';

/**
 * This zone's live CURSOR — the annotator's half of #73.
 *
 * The reader (`liveRead`, and the four rules that make it not a timer) lives in `@rask/api/live`.
 * What stays here is the half that cannot move: a cursor wraps THIS app's `feeds.remote.ts`, because
 * `query.live` is declared per app so it gets its own endpoint and can reach `getRequestEvent`.
 * Same seam `@rask/api/runs-feed` uses — share the body, keep the app-bound shim in the app.
 *
 * WHY THE ANNOTATOR NEEDS ONE. Its Arrow annotations were read exactly once, at mount. Two people
 * labelling the same page never saw each other's work, and neither did one person with the item open
 * in two tabs — the second save would be written against a `base_version` from before the first,
 * which is precisely the conflict OCC exists to refuse. Verified emitting: `annotations/commit.py`
 * calls `emit_save` on every real save (and in catalog mode the catalog emits for the same write),
 * so an annotation save DOES move this cursor.
 */
export { liveRead, type LiveCursor };

/**
 * The lineage cursor, from the ONE shared `lineageFeed` stream — the same feed the shell's
 * notification bell renders. SvelteKit dedupes identical live-query instances onto a single
 * connection, so the bell and every lineage-backed surface on the page ride one stream and cannot
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
