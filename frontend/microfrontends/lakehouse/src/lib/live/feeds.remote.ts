import { error } from '@sveltejs/kit';
import { getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import { fetchMe, parse } from '@rask/api';
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
 * The zone's LIVE CURSORS — `query.live` generators that say *when the estate changed*, so a panel can
 * re-read instead of waking on a timer.
 *
 * Before this, thirteen files in this zone ran `$effect(() => { load(); setInterval(load, 5000) })`. That
 * is a blind poll: it re-reads whether or not anything happened, every panel keeps its own clock (so two
 * views of the same entity disagree for up to 5s), and the cost is paid in full every tick. Measured on
 * the live estate as alice, through `:8090`:
 *
 *     GET /api/events?limit=200                 471_217 bytes
 *     GET /api/events?limit=200&summary=true     46_625 bytes
 *     GET /api/events?limit=1&summary=true          245 bytes   <- the cursor probe
 *     GET /api/runs                             330_103 bytes
 *
 * So the probe below is ~1900x cheaper than the read it gates, and on an idle estate the browser receives
 * nothing at all. The generator runs on the zone (SvelteKit/Bun) server and holds the cursor; SvelteKit
 * streams each yield and owns reconnect (exponential backoff + on `navigator.onLine`) — no hand-rolled
 * SSE, no client cursor bookkeeping. This is `admin/remote/admin.remote.ts`'s discipline (server-held
 * cursor, change-only yields) applied to the read side.
 *
 * Two sources, deliberately, and each surface picks the one that actually reports its changes:
 *
 *  - `lineageFeed` — `GET /events?limit=1&summary=true` on the lineage plane. **Governed per subject**:
 *    an event is returned only if the caller `can_get_metadata` on every dataset it references, so a
 *    non-admin gets a real live feed with no backend change and never learns that an event they cannot
 *    see exists. Everything data-shaped (tables, namespaces, datasets, jobs, runs, columns, models, the
 *    lineage DAG, the outbox backlog) hangs off this.
 *  - `controlCursor` — `GET /v1/events?since=` on the catalog control plane, which is **estate-admin
 *    gated** (`can_observe_events` on the FGA root) and reports *governance* mutations: warehouses,
 *    tenants, grants, promotions. Those never touch a dataset, so they never move the lineage cursor;
 *    the admin surfaces that render them use this instead.
 *
 * KEEPALIVE. A change-only stream is by definition silent on an idle estate, and both the edge
 * (`proxy-read-timeout`) and the zone's own Bun server (`IDLE_TIMEOUT`) sever an idle stream. So a cursor
 * is re-yielded every `KEEPALIVE_MS` even when it has not moved: the bytes keep the stream alive, and
 * because the yielded value is an unchanged *primitive*, `.current` is assigned an equal number and no
 * consumer's `$effect` re-runs. Traffic without a re-read.
 *
 * FAIL QUIET. A probe that 401s, 403s, times out or cannot connect does NOT throw and does not end the
 * stream — it simply does not tick. Every consuming panel already renders its own honest sign-in /
 * denied / offline state from its own read's status; a cursor that threw would replace that with a
 * boundary error and lose it. `jetstreamCursor` is the one exception, and says why.
 */

const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';
const NATS_MONITOR_API = env.NATS_MONITOR_API ?? '';

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// Parsed, not cast, at the wire boundary (the @rask/api parse-don't-validate rule) — a drift in the feed's
// shape must fail here rather than silently read `undefined` as "nothing changed" forever.
const ControlProbeSchema = v.object({ cursor: v.number(), reset: v.boolean() });
const JszProbeSchema = v.object({
	streams: v.number(),
	consumers: v.number(),
	messages: v.number(),
});

/** This zone's credential for a lineage read — the shared posture, given this request's session. */
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
 * the identities the admin surfaces are already behind.
 *
 * What it yields is a GENERATION, not the buffer head. A `reset` (the caller's cursor fell off the bounded,
 * drop-oldest buffer) is a change the console must re-read on, but it can hand back the very same head
 * number — and an unchanged primitive is exactly what the keepalive relies on NOT waking anyone. So every
 * real move increments a counter of our own, and the keepalive re-yields it unchanged.
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

/**
 * The JetStream cursor — deliberately NOT one of the two event feeds, and this is the surface where that
 * distinction matters most.
 *
 * `/admin/streams` exists to show a wedged or missing consumer. A wedged consumer emits no lineage events
 * and no control events, so driving this panel off either feed would leave it frozen in exactly the
 * incident it is there to catch. Its state is a gauge with no change-event source, so the honest live feed
 * is a server-side probe of the NATS monitor's own totals, yielded only when they move — still no browser
 * timer, still bytes only on change.
 *
 * Unlike the two feeds above this one is NOT self-gating: `/jsz` is unauthenticated by design and reachable
 * only in-cluster, and a remote function is a public HTTP endpoint, so the estate-admin door is made here —
 * the same `fetchMe` check `admin/+layout.server.ts` makes, failing closed on every ambiguity (no token, a
 * 401/403, a timeout, an unreachable catalog, contract drift). Terminal 403, exactly like `controlEvents`.
 */
export const jetstreamCursor = query.live(async function* (): AsyncGenerator<number> {
	const { fetch, locals } = getRequestEvent();
	if (locals.authEnabled) {
		const me = await fetchMe({
			catalogUrl: CATALOG_API,
			accessToken: locals.session?.accessToken,
			fetchFn: fetch,
		});
		if (!me?.estate_admin) error(403, 'the JetStream feed is estate-admin only');
	}
	if (!NATS_MONITOR_API) error(501, 'no NATS monitor is wired on this stack');

	let fingerprint = -1;
	let published = -1;
	let publishedAt = 0;
	for (;;) {
		try {
			const res = await fetch(`${NATS_MONITOR_API}/jsz`, {
				signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
			});
			if (res.ok) {
				const jsz = parse(JszProbeSchema, await res.json());
				// Totals only — the panel re-reads the trimmed, gated overview through its own BFF. Streams
				// and consumers ride along so a stream appearing or a consumer dying ticks even at a
				// standstill, which is the case this panel is for.
				fingerprint = jsz.messages * 1_000_000 + jsz.streams * 1000 + jsz.consumers;
			}
		} catch {
			/* fail quiet — the panel's own read renders the honest unavailable state */
		}
		const now = Date.now();
		if (fingerprint >= 0 && (fingerprint !== published || now - publishedAt >= KEEPALIVE_MS)) {
			published = fingerprint;
			publishedAt = now;
			yield fingerprint;
		}
		await sleep(POLL_MS);
	}
});

/**
 * THE lineage feed — the cursor every panel in this zone re-reads on, and the runs behind the shell's bell.
 *
 * The generator itself is `@rask/api/runs-feed`, shared with the other three zones: the bell shipped in one
 * zone out of four because the COMPONENT was shared and the TRANSPORT was not. What stays here is the
 * SvelteKit binding, which cannot be shared — `query.live` needs an endpoint in this app, and
 * `getRequestEvent` only exists inside it.
 */
export const lineageFeed = query.live(function (): AsyncGenerator<LineagePulse> {
	const { fetch } = getRequestEvent();
	return lineagePulse({ lineageApi: LINEAGE_API, fetch, headers: lineageHeaders });
});
