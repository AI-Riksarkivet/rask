import { liveRead, type LiveCursor } from '@rask/api/live';
import { controlCursor, lineageFeed } from './feeds.api';

/**
 * The console's live CURSORS. The reader itself — `liveRead`, and the rules that make it not a
 * timer — lives in `@rask/api/live`; what stays here wraps this app's own `feeds.api.ts`.
 *
 * Re-exported so consumers keep importing from one place: a surface asks the app for liveness
 * rather than reaching past it into a package.
 *
 * Pass a cursor (not a call of it) to `liveRead`, which opens it on mount:
 *
 *     liveRead(lineageTick, () => load());
 */
export { liveRead, type LiveCursor };

/** The lineage cursor, from the ONE shared `lineageFeed` stream, so every lineage-backed surface on
 *  a page advances on the same number. */
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

/** The CONTROL-plane cursor — for the surfaces whose changes are governance mutations (grants,
 *  warehouses, tenants) rather than data movement; those never move the lineage cursor. */
export function controlTick(): LiveCursor {
	const feed = controlCursor();
	return {
		get current() {
			return feed.current;
		},
		get connected() {
			return feed.connected;
		},
	};
}
