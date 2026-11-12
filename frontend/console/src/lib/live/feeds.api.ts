import * as v from 'valibot';
import { catalogJSON } from '#lib/doors.ts';
import { authorizedFetch } from '#lib/api/client.ts';
import { lineagePulse, POLL_MS, PROBE_TIMEOUT_MS, type LineagePulse } from '#lib/api/runs-feed.ts';
import { query } from '#lib/reads.svelte.ts';
import { page } from '$app/state';
import { isEstateAdmin } from '#lib/estate-admin.ts';
import { LINEAGE_API } from '#lib/upstreams.ts';
import { EventsEnvelopeSchema, readEvent, type FeedEntry } from '#lib/admin/control.ts';

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
 *  - `controlFeed` — `GET /management/v1/events?since=` on the catalog control plane, which is
 *    **estate-admin gated** (`can_observe_events` on the FGA root) and reports *governance* mutations:
 *    warehouses, tenants, grants. Those never touch a dataset, so they never move the lineage cursor.
 *
 * NEVER THROW. A probe that fails does not throw and does not end the stream; every consuming panel
 * renders its own honest sign-in / denied / offline state from its own read's status. The lineage
 * pulse simply does not tick; the control feed yields the failure for the events page to say, and
 * ends only on a 403.
 */

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** How many of the newest control events the feed keeps for the events page; older ones fall off. */
const WINDOW = 200;

/** What the control feed yields: a generation that moves on every change (a cursor for
 *  `liveRead`), the recent window newest first (the events page), and why the last poll failed. */
export interface ControlFeed {
	generation: number;
	entries: readonly FeedEntry[];
	/** The last poll's failure, or null once one answered: 0 unreachable, 502 a page that drifted
	 *  from the contract, else the catalog's status and its `detail`. A 403 ends the feed with it. */
	failure: { status: number; detail: string } | null;
}

/**
 * THE control-plane feed: one poller of `GET /management/v1/events?since=` for the whole page, read
 * by every surface that re-reads on a governance change (`controlTick`) and by the events page, which
 * renders the window it keeps. Estate-admin gated at the catalog, so it polls only for an estate admin.
 *
 * What it yields as `generation` is a counter of our own, not the buffer head. A `reset` (the
 * caller's cursor fell off the bounded, drop-oldest buffer) is a change the console must re-read on,
 * but it can hand back the very same head number. The window dedups by `event_id` and empties on a
 * reset, so a multi-replica catalog degrades noisily, never wrongly
 * (docs/adr/0028-control-events-per-replica-cursor-boundary.md).
 *
 * A failed poll is yielded as `failure` and the next poll is still made: the events page says what
 * went wrong, and every `controlTick` holder keeps its last answer and its own read's state. Only a
 * 403 ends the feed, since asking again would only repeat the catalog's answer.
 */
export const controlFeed = query.live(async function* (): AsyncGenerator<ControlFeed> {
	let cursor = 0;
	let generation = 0;
	let settled = false;
	let failure: ControlFeed['failure'] = null;
	const recent: FeedEntry[] = [];
	const seen = new Set<string>();
	const snapshot = (): ControlFeed => ({ generation, entries: recent.toReversed(), failure });
	for (;;) {
		// Polled only for an estate admin: the catalog refuses everyone else (authorization scope is the
		// feed's data scope), so a poll for them would be a refusal every few seconds and a cursor that
		// never moves; their pages read on mount and after their own writes. Asked on every tick, not
		// once: this feed lives as long as the page, and the root layout's `me` can arrive or change
		// after it opened (a re-run of the layout's load).
		if (!isEstateAdmin(page.data.me)) {
			await sleep(POLL_MS);
			continue;
		}
		const res = await catalogJSON(`/management/v1/events?since=${cursor}`, {
			signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
		});
		const envelope = res.ok ? v.safeParse(EventsEnvelopeSchema, res.data) : null;
		if (!res.ok || !envelope?.success) {
			const next = res.ok
				? { status: 502, detail: 'catalog contract drift: the events page' }
				: { status: res.status, detail: res.detail };
			const repeated = failure?.status === next.status && failure.detail === next.detail;
			failure = next;
			if (!repeated) yield snapshot();
			if (next.status === 403) return;
			await sleep(POLL_MS);
			continue;
		}
		const probe = envelope.output;
		let changed = probe.reset || probe.cursor !== cursor || failure !== null;
		failure = null;
		if (probe.reset) {
			recent.length = 0;
			seen.clear();
		}
		probe.events.forEach((raw, index) => {
			const entry = readEvent(raw, `unreadable:${probe.cursor}:${index}`);
			if (seen.has(entry.key)) return;
			seen.add(entry.key);
			recent.push(entry);
			changed = true;
		});
		cursor = probe.cursor;
		for (const dropped of recent.splice(0, Math.max(recent.length - WINDOW, 0)))
			seen.delete(dropped.key);
		// The first answer is the cursor's ARRIVAL, not a change: it settles every holder.
		if (settled && changed) generation++;
		if (!settled || changed) {
			settled = true;
			yield snapshot();
		}
		await sleep(POLL_MS);
	}
});

/** THE lineage feed — the cursor every data-shaped panel and the bell re-read on. */
export const lineageFeed = query.live(function (): AsyncGenerator<LineagePulse> {
	return lineagePulse({ lineageApi: LINEAGE_API, fetch: authorizedFetch });
});
