import { parse } from './parse.js';
import * as v from 'valibot';

/**
 * The lineage cursor pulse: probe the lineage feed's newest visible `seq` and yield it when it moves,
 * so a panel re-reads on a change instead of on a timer of its own.
 */

/** One beat of the lineage plane: where the estate's event log stands. */
export interface LineagePulse {
	/** The newest event `seq` this caller may see — the cursor every lineage-backed surface re-reads on. */
	cursor: number;
}

// Parsed, not cast, at the wire boundary (the `#lib/api` parse-don't-validate rule) — a drift in the feed's
// shape must fail here rather than silently read `undefined` as "nothing changed" forever.
const LineageProbeSchema = v.object({ events: v.array(v.object({ seq: v.number() })) });

/** How often the cursor is re-probed. */
export const POLL_MS = 5000;
/** A probe is a liveness signal, not a page load: bound it so a wedged upstream can't stall the loop. */
export const PROBE_TIMEOUT_MS = 4000;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export interface LineageFeedOptions {
	/** The lineage service's same-origin base, `/api/lineage`. */
	lineageApi: string;
	/** The fetch to use — `authorizedFetch`, so the signed-in person's bearer rides along. */
	fetch: (input: string, init?: RequestInit) => Promise<Response>;
}

/**
 * The lineage pulse generator: one stream carrying the cursor, opened once per page.
 *
 * FAIL QUIET. A probe that 401s, 403s, times out or cannot connect does NOT throw and does not end the
 * stream — it simply does not tick. Every consuming panel already renders its own honest sign-in / denied /
 * offline state from its own read's status; a cursor that threw would replace that with a boundary error.
 */
export async function* lineagePulse(opts: LineageFeedOptions): AsyncGenerator<LineagePulse> {
	const { lineageApi, fetch } = opts;

	/** The newest lineage `seq` THIS caller may see, or `null` when the probe did not resolve. */
	async function probe(): Promise<number | null> {
		try {
			const res = await fetch(`${lineageApi}/events?limit=1&summary=true`, {
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
	for (;;) {
		const seq = await probe();
		const moved = seq !== null && seq !== cursor;
		if (seq !== null) cursor = seq;
		// Yielded when the estate moved, or on the very first pass so consumers settle into an honest
		// empty state instead of hanging in `pending`.
		if (moved || !sent) {
			sent = true;
			yield { cursor: Math.max(cursor, 0) };
		}
		await sleep(POLL_MS);
	}
}
