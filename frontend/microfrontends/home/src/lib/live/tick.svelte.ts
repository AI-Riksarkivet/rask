import { liveRead, type LiveCursor } from '@rask/api/live';
import { controlCursor } from './feeds.remote';

/**
 * This zone's live CURSORS. The reader itself — `liveRead`, and the four rules that make it not a
 * timer — lives in `@rask/api/live`.
 *
 * `home` kept a LOCAL 89-line copy through the consolidation that moved lakehouse, models, compute
 * and annotator onto the shared one, and nothing noticed: both compile, both work, and they diverge
 * only on whichever rule the copy stops carrying. That is how the three pre-consolidation copies had
 * already drifted — only one still explained the open-on-mount measurement. Found by
 * `@rask/zone-contract`'s `transport-contract.test.ts`, which exists because two sessions extracted
 * this helper independently on the same day.
 *
 * What stays here is the half that CANNOT move: a cursor wraps this app's own `feeds.remote.ts`, and
 * `query.live` is declared per app so it gets its own endpoint and can reach `getRequestEvent`.
 *
 * Re-exported so this zone's consumers keep importing from one place — a surface asks its ZONE for
 * liveness rather than reaching past it into a package.
 */
export { liveRead, type LiveCursor };

/**
 * The CONTROL-plane cursor — for the surfaces whose changes are governance mutations (grants,
 * warehouses, tenants, promotions) rather than data movement. Those never touch a dataset, so they never
 * move the lineage cursor; `/settings/access` re-reads on this one instead. SvelteKit dedupes identical
 * live-query instances onto a single connection, so every control-backed surface on a page rides the
 * same stream and they cannot disagree about when the estate changed.
 *
 * Pass it (not a call of it) to `liveRead`, which opens it on mount:
 *
 *     liveRead(controlTick, () => load());
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
