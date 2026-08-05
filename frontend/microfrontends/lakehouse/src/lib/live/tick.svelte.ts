import { controlCursor, lineageFeed } from './feeds.remote';

// The MECHANISM moved to `@rask/ui/live-read` when a SECOND zone needed it — the moment a local
// helper stops being local and starts being duplication. The four rules that make it correct (first
// read unconditional, key tracked, cursor ARRIVAL is not a change, stream opened on mount) live with
// it there, each one carrying the test that broke when it was wrong. Only the BINDINGS below are
// per-zone, and they have to be: `query.live` must be declared inside an app to get its own endpoint.
import type { LiveCursor } from '@rask/ui/live-read';

export { liveRead, type LiveCursor } from '@rask/ui/live-read';

/**
 * The lineage cursor, taken from the ONE shared `lineageFeed` stream — the feed the shell's notification
 * bell also renders. SvelteKit dedupes identical live-query instances onto a single connection, so every
 * lineage-backed surface on a page and the bell above it ride the same stream, advance on the same number,
 * and cannot disagree about when the estate changed.
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

/**
 * The CONTROL-plane cursor, same contract — for the surfaces whose changes are governance mutations
 * (grants, warehouses, tenants, promotions) rather than data movement. Those never touch a dataset, so
 * they never move the lineage cursor; the FGA workbench re-reads on this one instead. The stream is
 * shared with the admin console's own consumers, so a grant landing anywhere on the estate advances
 * every surface on the same number.
 */
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
