import { parse } from './parse.js';
import * as v from 'valibot';

/**
 * The lineage cursor pulse: follow the lineage feed forward and yield when an event this caller may
 * see arrives, so a panel re-reads on a change instead of on a timer of its own.
 */

/** One beat of the lineage plane: a number that moves when the estate's event log moved for this caller. */
export interface LineagePulse {
	/** A position in the feed at or after the newest event this caller may see: the cursor every
	 *  lineage-backed surface re-reads on. It is compared, never read as an event's own seq. */
	cursor: number;
}

// Parsed, not cast, at the wire boundary (the `#lib/api` parse-don't-validate rule) — a drift in the feed's
// shape must fail here rather than silently read `undefined` as "nothing changed" forever. A `since` read
// always answers the `since` to pass next (docs/COMMIT-EVENTS.md), so `next_cursor` is a number here.
const FollowPageSchema = v.object({
	events: v.array(v.object({ seq: v.number() })),
	next_cursor: v.number(),
});

/** How often the feed is followed. */
export const POLL_MS = 5000;
/** A probe is a liveness signal, not a page load: bound it so a wedged upstream can't stall the loop. */
export const PROBE_TIMEOUT_MS = 4000;

/**
 * A `since` no feed has issued. The feed answers a cursor past its newest seq with no events and
 * `next_cursor` equal to that newest seq (docs/COMMIT-EVENTS.md), reading and judging no row, so this
 * asks where the feed's head is.
 */
const PAST_THE_HEAD = Number.MAX_SAFE_INTEGER;

export interface LineageFeedOptions {
	/** The lineage service's same-origin base, `/api/lineage`. */
	lineageApi: string;
	/** The fetch to use — `authorizedFetch`, so the signed-in person's bearer rides along. */
	fetch: (input: string, init?: RequestInit) => Promise<Response>;
	/** The wait between polls; a test passes its own. */
	sleep?: (ms: number) => Promise<void>;
}

/** What one read of the feed after a cursor answered. */
interface Followed {
	/** The `since` to pass next: the last seq the read covered, rows the caller may not see included. */
	next: number;
	/** Whether an event the caller may see lies after the cursor. */
	visible: boolean;
}

/**
 * The lineage pulse generator: one stream carrying the cursor, opened once per page.
 *
 * FOLLOWED FORWARD, never re-read from the top. The feed is governed per caller, and on an estate with
 * more than one tenant most of its newest rows belong to someone else. A newest-first probe judges
 * those rows again on every poll; a follower passes each row once, and a poll that finds nothing new
 * reads no row at all. Measured on the dev estate 2026-11-13 with about 490 rows a caller could not see
 * above her newest visible one: the newest-first probe took 1.2 to 1.7 s on every poll, a `since` read
 * at the head 0.25 to 0.30 s.
 *
 * ONE PULSE PER BURST. When a visible event arrives the follower asks for the head again and stands
 * there, so a burst of events is one pulse, not one every poll until it is drained: each pulse makes
 * every panel re-read, and that read sees the whole burst. The pulse is yielded after the head is
 * read, so no event can fall between the two.
 *
 * FAIL QUIET. A read that 401s, 403s, times out or cannot connect does NOT throw and does not end the
 * stream — it simply does not tick. Every consuming panel already renders its own honest sign-in / denied /
 * offline state from its own read's status; a cursor that threw would replace that with a boundary error.
 */
export async function* lineagePulse(opts: LineageFeedOptions): AsyncGenerator<LineagePulse> {
	const { lineageApi, fetch } = opts;
	const sleep =
		opts.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));

	/** What lies after `since` for THIS caller, or `null` when the read did not resolve. */
	async function follow(since: number): Promise<Followed | null> {
		try {
			const res = await fetch(`${lineageApi}/events?since=${since}&limit=1&summary=true`, {
				signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
			});
			if (!res.ok) return null;
			const page = parse(FollowPageSchema, await res.json());
			return { next: page.next_cursor, visible: page.events.length > 0 };
		} catch {
			return null;
		}
	}

	// Where the follower stands; `null` until the feed has said where its head is.
	let since: number | null = null;
	for (;;) {
		if (since === null) {
			const head = await follow(PAST_THE_HEAD);
			if (head !== null) {
				since = head.next;
				// The first answer is the cursor's ARRIVAL: it settles every consumer into an honest
				// state instead of leaving it in `pending`. An empty feed answers 0.
				yield { cursor: since };
			}
		} else {
			const page = await follow(since);
			if (page !== null) {
				since = page.next;
				if (page.visible) {
					since = (await follow(PAST_THE_HEAD))?.next ?? since;
					yield { cursor: since };
				}
			}
		}
		await sleep(POLL_MS);
	}
}
