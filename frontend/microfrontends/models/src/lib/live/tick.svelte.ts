import { liveRead, type LiveCursor } from '@rask/api/live';
import { lineageFeed } from './feeds.remote';

/**
 * This zone's live CURSORS. The reader itself — `liveRead`, and the four rules that make it not a
 * timer — moved to `@rask/api/live`, because it had been written three times (home, lakehouse,
 * models; 274 lines between them) and the three copies had already diverged: only one still carried
 * the measurement behind the open-on-mount rule.
 *
 * What stays here is the half that CANNOT move: a cursor wraps this app's own `feeds.remote.ts`, and
 * `query.live` is declared per app so it gets its own endpoint and can reach `getRequestEvent`. Same
 * seam `@rask/api/runs-feed` already uses — share the body, keep the app-bound shim in the app.
 *
 * `liveRead` and `LiveCursor` are re-exported so the ~18 consumers keep importing from one place:
 * a surface should ask its ZONE for liveness, not reach past it into a package.
 */
export { liveRead, type LiveCursor };


/**
 * The lineage cursor, taken from the ONE shared `lineageFeed` stream — the feed the shell's
 * notification bell also renders. SvelteKit dedupes identical live-query instances onto a single
 * connection, so every lineage-backed surface on a page and the bell above it ride the same stream,
 * advance on the same number, and cannot disagree about when the estate changed.
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
