import { parse } from './parse.js';
import * as v from 'valibot';

/**
 * The lineage cursor pulse, shared by every zone.
 *
 * The generator lives here rather than in each zone because it is entirely generic: probe the lineage
 * cursor and keep the stream alive. What is NOT generic is the SvelteKit binding — `query.live` must be
 * declared inside each app to get its own endpoint, and `getRequestEvent` only exists there. So each zone
 * owns a four-line `.remote.ts` that hands this its `fetch` and its credential, and nothing else is
 * duplicated.
 */

/** One beat of the lineage plane: where the estate's event log stands. */
export interface LineagePulse {
	/** The newest event `seq` this caller may see — the cursor every lineage-backed surface re-reads on. */
	cursor: number;
}

// Parsed, not cast, at the wire boundary (the @rask/api parse-don't-validate rule) — a drift in the feed's
// shape must fail here rather than silently read `undefined` as "nothing changed" forever.
const LineageProbeSchema = v.object({ events: v.array(v.object({ seq: v.number() })) });

/** How often the server re-probes its cursor. The browser only hears about it when it moved. */
export const POLL_MS = 5000;
/** Re-yield an unmoved cursor at least this often, so an idle stream is never severed as idle. */
export const KEEPALIVE_MS = 20_000;
/** A probe is a liveness signal, not a page load: bound it so a wedged upstream can't stall the loop. */
export const PROBE_TIMEOUT_MS = 4000;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export interface LineageFeedOptions {
	/** The lineage service's base URL, from the zone's own private env. */
	lineageApi: string;
	/** The request-scoped fetch (SvelteKit's, so the zone's own instrumentation applies). */
	fetch: typeof globalThis.fetch;
	/** The credential for this caller — the user's bearer, or the read-only service token as a bearer. */
	headers: () => Record<string, string>;
}

/**
 * The lineage pulse generator: one stream carrying the cursor, opened once per page in the root layout.
 *
 * FAIL QUIET. A probe that 401s, 403s, times out or cannot connect does NOT throw and does not end the
 * stream — it simply does not tick. Every consuming panel already renders its own honest sign-in / denied /
 * offline state from its own read's status; a cursor that threw would replace that with a boundary error.
 *
 * KEEPALIVE. A change-only stream is by definition silent on an idle estate, and both the edge
 * (`proxy-read-timeout`) and the zone's own Bun server (`IDLE_TIMEOUT`) sever an idle stream. So the last
 * pulse is re-sent every {@link KEEPALIVE_MS}: the bytes keep the stream alive, and because `cursor` is
 * then an unchanged number, no consumer's `$effect` re-runs on it.
 */
export async function* lineagePulse(opts: LineageFeedOptions): AsyncGenerator<LineagePulse> {
	const { lineageApi, fetch, headers } = opts;

	/** The newest lineage `seq` THIS caller may see, or `null` when the probe did not resolve. */
	async function probe(): Promise<number | null> {
		try {
			const res = await fetch(`${lineageApi}/events?limit=1&summary=true`, {
				headers: headers(),
				signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
			});
			if (!res.ok) return null;
			// An empty visible feed is a real answer (cursor 0), not a failure — it must settle the consumer.
			return parse(LineageProbeSchema, await res.json()).events[0]?.seq ?? 0;
		} catch {
			return null;
		}
	}

	let cursor = -1;
	let sent = false;
	let sentAt = 0;
	for (;;) {
		const seq = await probe();
		const moved = seq !== null && seq !== cursor;
		if (seq !== null) cursor = seq;
		const now = Date.now();
		// Yielded when the estate moved, or on the very first pass so consumers settle into an honest
		// empty state instead of hanging in `pending`.
		if (moved || !sent || now - sentAt >= KEEPALIVE_MS) {
			sent = true;
			sentAt = now;
			yield { cursor: Math.max(cursor, 0) };
		}
		await sleep(POLL_MS);
	}
}

/**
 * The credential for a lineage read, and the one `makeBackendProxy` attaches to every read: the signed-in
 * user's bearer when there is one, else the READ-only service token (the pod's projected `rask-lineage`
 * ServiceAccount token, D1) as a bearer. The lineage door takes the subject from the verified token, so
 * nothing else names the caller. The caller reads the token file per request (`readSecretFile`), because
 * kubelet rotates it before its 600 s expiry. Anything else (auth-off dev) sends nothing.
 */
export function lineageAuthHeaders(input: {
	accessToken?: string | null | undefined;
	serviceToken?: string | undefined;
}): Record<string, string> {
	const bearer = input.accessToken || input.serviceToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}
