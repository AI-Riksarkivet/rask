import { parse } from './parse.js';
import * as v from 'valibot';

/**
 * The run-notification pulse, shared by every zone.
 *
 * `@rask/ui`'s `NotificationCenter` was written so that "every zone gets the SAME one" — the annotator had
 * already forked the header once and drifted. But the *transport* stayed in the lakehouse zone, so the bell
 * shipped in one zone out of four. Measured live, signed in as alice:
 *
 *     bell in home: 0 · lakehouse: 1 · media: 0 · annotator: 0
 *
 * Which is the wrong one to have. A person waiting on a batch is most likely to be in the annotator or the
 * media zone, doing the work the batch feeds — not on the run board, which is where they would have to go
 * to find out it failed. The component being shared was never the point; being *mounted* is.
 *
 * The generator lives here rather than in each zone because it is entirely generic: probe the lineage
 * cursor, re-read `/runs` when it moves, sort failures first, trim to the window, keep the stream alive.
 * What is NOT generic is the SvelteKit binding — `query.live` must be declared inside each app to get its
 * own endpoint, and `getRequestEvent` only exists there. So each zone owns a four-line `.remote.ts` that
 * hands this its `fetch` and its credential, and nothing else is duplicated.
 */

/** One run as the notification surface renders it — the `/runs` row minus everything a bell never shows. */
export interface RunNotice {
	run_id: string;
	/** Who ran it. Dropped from the first version of this trim, and the bell rendered "unknown author" on
	 *  every row — a field the surface displays is not an optional field. Caught by looking at the
	 *  screenshot (`docs/audits/shots/live-notification-bell.png`), not by any assertion. */
	author: string | null;
	job: string | null;
	state: string | null;
	progress_done: number | null;
	progress_total: number | null;
	error_message: string | null;
	updated_at: string | null;
	operation: string | null;
	/** WHY a promotion did not advance — `HELD` / `BLOCKED` / `REFUSED`. `state` is `FAIL` for all
	 *  three, so without this a hold is indistinguishable from an outage. */
	promotion_status: string | null;
}

/** One beat of the lineage plane: where the estate's event log stands, and the runs behind the bell. */
export interface LineagePulse {
	/** The newest event `seq` this caller may see — the cursor every lineage-backed surface re-reads on. */
	cursor: number;
	/** The most recently active runs, trimmed to {@link NOTICE_WINDOW} on the server. */
	runs: RunNotice[];
}

// Parsed, not cast, at the wire boundary (the @rask/api parse-don't-validate rule) — a drift in the feed's
// shape must fail here rather than silently read `undefined` as "nothing changed" forever.
const LineageProbeSchema = v.object({ events: v.array(v.object({ seq: v.number() })) });
const RunsSchema = v.object({
	runs: v.array(
		v.object({
			run_id: v.string(),
			job: v.nullish(v.string()),
			author: v.nullish(v.string()),
			state: v.nullish(v.string()),
			progress_done: v.nullish(v.number()),
			progress_total: v.nullish(v.number()),
			error_message: v.nullish(v.string()),
			updated_at: v.nullish(v.string()),
			operation: v.nullish(v.string()),
			promotion_status: v.nullish(v.string()),
		}),
	),
});

/** How many runs the bell keeps. A notification surface is a recency window, not the run board. */
export const NOTICE_WINDOW = 20;
/** How often the server re-probes its cursor. The browser only hears about it when it moved. */
export const POLL_MS = 5000;
/** Re-yield an unmoved cursor at least this often, so an idle stream is never severed as idle. */
export const KEEPALIVE_MS = 20_000;
/** A probe is a liveness signal, not a page load: bound it so a wedged upstream can't stall the loop. */
export const PROBE_TIMEOUT_MS = 4000;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const isFailed = (r: {
	state?: string | null | undefined;
	promotion_status?: string | null | undefined;
}): boolean => {
	const s = (r.state ?? '').toUpperCase();
	if (s !== 'FAIL' && s !== 'ABORT') return false;
	// A HELD promotion arrives as FAIL and is a question for a validator, not a failure. It must not
	// sort into the failures-first block: measured on the live board, every hold sat at the top of
	// the list looking like an outage. BLOCKED and REFUSED stay failures — no approval waives them.
	return (r.promotion_status ?? '').toUpperCase() !== 'HELD';
};

/**
 * FAILURES FIRST, then newest — and this ordering has to happen where the list is CUT, not in the
 * component. Measured against the live estate: 891 runs with the first FAIL at position 445, so a
 * recency-only trim to 20 handed the bell twenty successes and the panel could not have shown a failure
 * however it sorted them. Reordering the component alone changed nothing, which is what made the layering
 * obvious. Exported so a test can pin the rule without a live estate.
 */
export function orderForNotice<
	T extends { state?: string | null | undefined; updated_at?: string | null | undefined },
>(runs: T[], limit = NOTICE_WINDOW): T[] {
	return runs
		.slice()
		.sort((a, b) => {
			if (isFailed(a) !== isFailed(b)) return isFailed(a) ? -1 : 1;
			return (b.updated_at ?? '').localeCompare(a.updated_at ?? '');
		})
		.slice(0, limit);
}

export interface LineageFeedOptions {
	/** The lineage service's base URL, from the zone's own private env. */
	lineageApi: string;
	/** The request-scoped fetch (SvelteKit's, so the zone's own instrumentation applies). */
	fetch: typeof globalThis.fetch;
	/** The credential for this caller — the user's bearer, or the read-only service identity. */
	headers: () => Record<string, string>;
}

/**
 * The lineage pulse generator: one stream carrying both the cursor and the notification runs, because a
 * zone needs exactly these two things and needs them to agree.
 *
 * It was two live queries at first: a cursor for the panels and a run feed for the shell's bell. Both are
 * mounted on every page (the bell lives in the root layout), so every page held two long-lived streams to
 * the same origin, each polling the same `/events` probe on its own clock — measured at ~30 probes/second
 * against the e2e suite. Merging them halves that and removes a subtler problem for free: with separate
 * cursors the bell and the table under it could be a poll apart on the same estate.
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

	async function readRuns(): Promise<RunNotice[] | null> {
		try {
			const res = await fetch(`${lineageApi}/runs`, {
				headers: headers(),
				signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
			});
			if (!res.ok) return null;
			const board: v.InferOutput<typeof RunsSchema> = parse(RunsSchema, await res.json());
			// Trimmed HERE, not in the browser: `/runs` measured 330_103 bytes on the live estate (875 runs)
			// and a bell renders the newest NOTICE_WINDOW rows and the nine fields it displays.
			return orderForNotice<v.InferOutput<typeof RunsSchema>['runs'][number]>(board.runs).map(
				(r) => ({
					run_id: r.run_id,
					job: r.job ?? null,
					author: r.author ?? null,
					state: r.state ?? null,
					progress_done: r.progress_done ?? null,
					progress_total: r.progress_total ?? null,
					error_message: r.error_message ?? null,
					updated_at: r.updated_at ?? null,
					operation: r.operation ?? null,
					promotion_status: r.promotion_status ?? null,
				}),
			);
		} catch {
			return null;
		}
	}

	let cursor = -1;
	let sent = false;
	let sentAt = 0;
	let last: RunNotice[] = [];
	for (;;) {
		const seq = await probe();
		const moved = seq !== null && seq !== cursor;
		if (seq !== null) cursor = seq;
		const now = Date.now();
		// The run board is re-read only when the estate moved (or on the very first pass, so consumers
		// settle into an honest empty state instead of hanging in `pending`).
		if (moved || !sent) {
			const runs = await readRuns();
			if (runs !== null) last = runs;
			if (runs !== null || !sent) {
				sent = true;
				sentAt = now;
				yield { cursor: Math.max(cursor, 0), runs: last };
			}
		} else if (now - sentAt >= KEEPALIVE_MS) {
			sentAt = now;
			yield { cursor: Math.max(cursor, 0), runs: last };
		}
		await sleep(POLL_MS);
	}
}

/**
 * The credential for a lineage read, mirroring `makeLineageProxy` exactly: the signed-in user's bearer when
 * there is one, else the READ-only service credential a governed stack uses to serve reads without a
 * per-user login. Anything else (auth-off dev) sends nothing.
 */
export function lineageAuthHeaders(input: {
	accessToken?: string | null;
	serviceToken?: string;
	serviceId?: string;
}): Record<string, string> {
	if (input.accessToken) return { authorization: `Bearer ${input.accessToken}` };
	if (input.serviceToken) {
		return {
			'dapr-api-token': input.serviceToken,
			'x-lance-service-identity': input.serviceId ?? '',
		};
	}
	return {};
}
