import { parse } from '#lib/api/index.ts';
import { authorizedFetch } from '#lib/api/client.ts';
import { lineagePulse, POLL_MS, PROBE_TIMEOUT_MS, type LineagePulse } from '#lib/api/runs-feed.ts';
import * as v from 'valibot';
import { query } from '#lib/reads.svelte.ts';
import { CATALOG_API, LINEAGE_API } from '#lib/upstreams.ts';

export type { LineagePulse } from '#lib/api/runs-feed.ts';

/**
 * The console's LIVE CURSORS — generators that say *when the estate changed*, so a panel re-reads
 * instead of waking on a timer of its own. Each runs once in the browser and every surface on the page
 * reads the same instance, so two views of one entity advance on the same number.
 *
 * Two event sources, deliberately, and each surface picks the one that actually reports its changes:
 *
 *  - `lineageFeed` — `GET /events?limit=1&summary=true` on the lineage plane. **Governed per subject**:
 *    an event is returned only if the caller `can_get_metadata` on every dataset it references, so a
 *    non-admin gets a real live feed and never learns that an event they cannot see exists. Everything
 *    data-shaped (tables, namespaces, runs, the lineage DAG, the outbox backlog) hangs off this.
 *  - `controlCursor` — `GET /management/v1/events?since=` on the catalog control plane, which is
 *    **estate-admin gated** (`can_observe_events` on the FGA root) and reports *governance* mutations:
 *    warehouses, tenants, grants. Those never touch a dataset, so they never move the lineage cursor.
 *
 * FAIL QUIET. A probe that 401s, 403s, times out or cannot connect does NOT throw and does not end the
 * stream — it simply does not tick. Every consuming panel renders its own honest sign-in / denied /
 * offline state from its own read's status.
 */

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// Parsed, not cast, at the wire boundary — a drift in the feed's shape must fail here rather than
// silently read `undefined` as "nothing changed" forever.
const ControlProbeSchema = v.object({ cursor: v.number(), reset: v.boolean() });

/**
 * The catalog control-plane cursor: the head of the catalog's change-event ring buffer, yielded when
 * it moves. Estate-admin gated at the catalog, so this ticks only for the identities the admin and
 * settings surfaces are already behind.
 *
 * What it yields is a GENERATION, not the buffer head. A `reset` (the caller's cursor fell off the
 * bounded, drop-oldest buffer) is a change the console must re-read on, but it can hand back the very
 * same head number, so every real move increments a counter of our own.
 */
export const controlCursor = query.live(async function* (): AsyncGenerator<number> {
	let cursor = 0;
	let generation = 0;
	let settled = false;
	for (;;) {
		let moved = false;
		try {
			const res = await authorizedFetch(`${CATALOG_API}/management/v1/events?since=${cursor}`, {
				signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
			});
			if (res.ok) {
				const probe = parse(ControlProbeSchema, await res.json());
				const first = !settled;
				moved = settled && (probe.reset || probe.cursor !== cursor);
				cursor = probe.cursor;
				settled = true;
				if (first) yield generation;
			}
		} catch {
			/* fail quiet — the panel's own read renders the honest denied/offline state */
		}
		if (moved) {
			generation++;
			yield generation;
		}
		await sleep(POLL_MS);
	}
});

/** THE lineage feed — the cursor every data-shaped panel and the bell re-read on. */
export const lineageFeed = query.live(function (): AsyncGenerator<LineagePulse> {
	return lineagePulse({ lineageApi: LINEAGE_API, fetch: authorizedFetch });
});
