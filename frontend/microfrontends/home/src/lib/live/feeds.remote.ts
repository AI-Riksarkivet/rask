import { getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import { parse } from '@rask/api';
import {
	KEEPALIVE_MS,
	lineageAuthHeaders,
	lineagePulse,
	POLL_MS,
	PROBE_TIMEOUT_MS,
	type LineagePulse,
} from '@rask/api/runs-feed';
import * as v from 'valibot';

export type { LineagePulse, RunNotice } from '@rask/api/runs-feed';

/**
 * This zone's LIVE feeds — the run-notification bell, and the control-plane cursor the estate-settings
 * surfaces re-read on.
 *
 * The whole body of `lineageFeed` is `@rask/api/runs-feed`, shared with every other zone — probe the
 * lineage cursor, re-read `/runs` when it moves, failures first, trim to the window, keep the stream
 * alive. What cannot be shared is exactly this file: `query.live` must be declared inside an app to get
 * its own endpoint, and `getRequestEvent` only exists there. So a zone's cost of having the bell is four
 * lines, which is why it had no business shipping in one zone out of four.
 *
 * Two sources, deliberately, and each surface picks the one that actually reports its changes:
 *
 *  - `lineageFeed` — `GET /events?limit=1&summary=true` on the lineage plane, governed per subject.
 *    Everything data-shaped hangs off this.
 *  - `controlCursor` — `GET /v1/events?since=` on the catalog control plane, which is **estate-admin
 *    gated** (`can_observe_events` on the FGA root) and reports *governance* mutations: warehouses,
 *    tenants, grants, promotions. Those never touch a dataset, so they never move the lineage cursor;
 *    `/settings/access` and `/settings/audit` use this instead.
 */
const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// Parsed, not cast, at the wire boundary (the @rask/api parse-don't-validate rule) — a drift in the feed's
// shape must fail here rather than silently read `undefined` as "nothing changed" forever.
const ControlProbeSchema = v.object({ cursor: v.number(), reset: v.boolean() });

function lineageHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	return lineageAuthHeaders({
		accessToken: locals.session?.accessToken,
		serviceToken: env.LINEAGE_SERVICE_TOKEN,
		serviceId: env.LINEAGE_SERVICE_ID,
	});
}

/**
 * The catalog control-plane cursor: the head of this replica's change-event ring buffer, yielded when it
 * moves. Estate-admin gated at the catalog (`can_observe_events` on the FGA root), so this ticks only for
 * the identities the settings surfaces are already behind.
 *
 * What it yields is a GENERATION, not the buffer head. A `reset` (the caller's cursor fell off the bounded,
 * drop-oldest buffer) is a change the console must re-read on, but it can hand back the very same head
 * number — and an unchanged primitive is exactly what the keepalive relies on NOT waking anyone. So every
 * real move increments a counter of our own, and the keepalive re-yields it unchanged.
 *
 * KEEPALIVE. A change-only stream is by definition silent on an idle estate, and both the edge
 * (`proxy-read-timeout`) and the zone's own Bun server (`IDLE_TIMEOUT`) sever an idle stream. So the cursor
 * is re-yielded every `KEEPALIVE_MS` even when it has not moved: the bytes keep the stream alive, and
 * because the yielded value is an unchanged *primitive*, `.current` is assigned an equal number and no
 * consumer's `$effect` re-runs. Traffic without a re-read.
 *
 * FAIL QUIET. A probe that 401s, 403s, times out or cannot connect does NOT throw and does not end the
 * stream — it simply does not tick. Every consuming panel already renders its own honest sign-in /
 * denied / offline state from its own read's status; a cursor that threw would replace that with a
 * boundary error and lose it.
 */
export const controlCursor = query.live(async function* (): AsyncGenerator<number> {
	const { fetch, locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	const headers: Record<string, string> = bearer ? { authorization: `Bearer ${bearer}` } : {};

	let cursor = 0;
	let generation = 0;
	let settled = false;
	let publishedAt = 0;
	for (;;) {
		let moved = false;
		try {
			const res = await fetch(`${CATALOG_API}/v1/events?since=${cursor}`, {
				headers,
				signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
			});
			if (res.ok) {
				const probe = parse(ControlProbeSchema, await res.json());
				moved = settled && (probe.reset || probe.cursor !== cursor);
				cursor = probe.cursor;
				settled = true;
			}
		} catch {
			/* fail quiet — the panel's own read renders the honest denied/offline state */
		}
		const now = Date.now();
		if (moved) generation++;
		if (settled && (moved || publishedAt === 0 || now - publishedAt >= KEEPALIVE_MS)) {
			publishedAt = now;
			yield generation;
		}
		await sleep(POLL_MS);
	}
});

export const lineageFeed = query.live(function (): AsyncGenerator<LineagePulse> {
	const { fetch } = getRequestEvent();
	return lineagePulse({ lineageApi: LINEAGE_API, fetch, headers: lineageHeaders });
});
