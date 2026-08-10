/**
 * The notification inbox transport — the backend half of the shared bell's persistence seam.
 *
 * `@rask/ui`'s `NotificationCenter` has always exposed `seen`/`dismissed` as bindable props with
 * `onseen`/`ondismiss` documented at `notification-center.svelte:44-48` as "the persistence seam".
 * Unbound, the component remembers per TAB: close the tab and a FAILED run you had already dealt
 * with comes back unread, on every zone, forever. This module is what a zone binds that seam to —
 * `services/notifications`' inbox, one actor per subject, reached through the gateway's
 * `/api/notifications` row.
 *
 * **Every route here is SUBJECT-DERIVED and takes no subject.** The service mints the actor id from
 * the verified token's `sub` with no header fallback, so the only thing this transport can address
 * is the caller's own inbox — which is also why it is server-side only: the bearer lives in the
 * sealed httpOnly session cookie a zone's BFF holds, and the browser cannot attach it.
 *
 * Built on `@rask/api/upstream` rather than a seventeenth hand-rolled fetch ladder (#93): the
 * three-outcome `ApiResult` contract — `0` unreachable, `n` answered-n, `502` contract drift — is
 * what every caller's honest-state branch reads, and the drift class that produced sixteen
 * disagreeing copies is exactly the one a new transport re-opens.
 */

import * as v from 'valibot';

import type { ApiResult } from './client.js';
import { parsed, upstreamJSON } from './upstream.js';

/** Names this upstream in every failure detail — "answered 503" alone sends a reader to the wrong
 *  service, and the estate now runs sixteen app-ids. */
const UPSTREAM = 'notifications';

/**
 * One stored notification, as `GET /inbox` returns it.
 *
 * A POINTER, never a payload copy: an id, why you were told, the one governed object it names, the
 * run behind it, and this subject's relationship to it. The body a reader eventually sees is
 * re-read through the governed path at render time, which is what keeps a revoked grant from being
 * readable out of somebody's inbox.
 */
export interface InboxNotification {
	/** `run_id@STATE` — the id the shared bell already keys seen/dismissed by (`runNotificationId`),
	 *  which is what makes dismissing "started" still let "failed" through. */
	notification_id: string;
	/** Why this subject was told. Read as an OPEN string, not a closed set: v2 (project watch) and
	 *  v3 (governance) add members, and a client that pinned today's single `author` would stop
	 *  parsing the whole feed the day the backend gained a reason — for a field nothing renders yet. */
	reason: string;
	/** The governed object the visibility check ran against (a dataset name, checked as `table:<id>`). */
	object_id: string;
	/** The producer's OWN run id — what lineage's detail doors answer to. The graph run id is a
	 *  derived uuid5 and links to nothing. */
	source_run_id: string | null;
	/** The lineage feed sequence this arrived on, when it came over the reconciler rather than the bus. */
	event_seq: number | null;
	occurred_at: string;
	seen: boolean;
	/** Declared because the wire declares it — but it can never be `true` through this door today:
	 *  the service drops dismissed rows from BOTH filters (`services/notifications/.../feed.py`
	 *  `visible()`), so a dismissed notification is absent rather than present-and-flagged. See
	 *  {@link inboxReadState}, which is where that costs something. */
	dismissed: boolean;
}

/** One page of a subject's inbox, plus the badge for the whole of it. */
export interface InboxFeed {
	notifications: InboxNotification[];
	/** Absent exactly when the server knows there is nothing after this page — a client stops on
	 *  `null`, never on a short page. */
	next_cursor: string | null;
	/** Counts the INBOX, not the page: the badge is a property of the inbox, so it does not shrink
	 *  as a reader pages. */
	unread: number;
}

/** What `POST /inbox/seen` changed. `updated` counts CHANGES, not ids handed in — re-sending an
 *  already-read set is a no-op that reports zero. */
export interface MarkResult {
	updated: number;
	unread: number;
}

/** What `POST /inbox/dismiss` changed. */
export interface DismissResult {
	dismissed: number;
	unread: number;
}

/** What the shared bell's Inbox tab renders: one page of rows plus the badge for the WHOLE inbox.
 *
 *  Two fields rather than the raw `InboxFeed` because the cursor is deliberately not part of the
 *  bell's contract — the panel shows a window and says how many more there are, it does not page.
 *  `null` at the call site (see each zone's `inbox.remote.ts`) is the un-wired case: no session, no
 *  service, and the bell falls back to its own per-tab memory rather than to a blank panel. */
export interface InboxPanel {
	rows: InboxNotification[];
	unread: number;
}

/** Project a feed onto the panel's shape. Trivial, and shared anyway: seven zones derive this, and
 *  the one that derived it differently would be the one whose badge disagreed with the others. */
export function inboxPanel(feed: InboxFeed): InboxPanel {
	return { rows: feed.notifications, unread: feed.unread };
}

/** `unread` is what the badge counts, `all` is what the panel shows. Neither includes dismissed rows. */
export type InboxFilter = 'unread' | 'all';

/** The service's own page ceiling (`INBOX_PAGE_LIMIT_MAX`), mirrored so a caller cannot ask for a
 *  page the store refuses to serve and get a 422 for its trouble. */
export const INBOX_PAGE_LIMIT_MAX = 100;

/** `string | null` on the way out, and tolerant of an absent key on the way in — a wire field the
 *  producer omits and one it sends as `null` mean the same thing to a reader. */
const nullableString = v.optional(v.nullable(v.string()), null);
const nullableNumber = v.optional(v.nullable(v.number()), null);

// Parsed, not cast, at the wire boundary (the @rask/api parse-don't-validate rule): a drift in the
// inbox's shape must surface as a 502-flavoured `ApiResult` the caller can render, never as
// `undefined` read as "nothing to show" forever. The annotations tie each schema to the interface
// above, so a field renamed in one and not the other fails `check` rather than at runtime.
const NotificationSchema: v.GenericSchema<unknown, InboxNotification> = v.object({
	notification_id: v.string(),
	reason: v.string(),
	object_id: v.string(),
	source_run_id: nullableString,
	event_seq: nullableNumber,
	occurred_at: v.string(),
	seen: v.boolean(),
	dismissed: v.boolean(),
});

const InboxFeedSchema: v.GenericSchema<unknown, InboxFeed> = v.object({
	notifications: v.array(NotificationSchema),
	next_cursor: nullableString,
	unread: v.number(),
});

const MarkResultSchema: v.GenericSchema<unknown, MarkResult> = v.object({
	updated: v.number(),
	unread: v.number(),
});

const DismissResultSchema: v.GenericSchema<unknown, DismissResult> = v.object({
	dismissed: v.number(),
	unread: v.number(),
});

/** One inbox call. A parameter object because the zone-bound halves (`fetch`, the bearer) and the
 *  env-bound half (`base`) come from three different places and three positionals would be F1 soup. */
export interface InboxRequest {
	/** The REQUEST EVENT's fetch, so the zone's own instrumentation and server-side cookies ride along. */
	fetch: typeof globalThis.fetch;
	/** The notifications API base — the gateway's `/api/notifications` row, env-resolved in the zone.
	 *  The gateway rewrites that prefix to itself, so the service's own paths hang off it unchanged. */
	base: string;
	/** The signed-in session's bearer. Absent on an auth-off stack, where the service resolves the
	 *  subject as `anon` and serves one anonymous inbox — the honest local shape, not a special case. */
	bearer?: string | undefined;
}

/** Which page to ask for. All three are optional: the service owns the default filter and the
 *  default page size (a `pydantic-settings` field, so measuring it later is a config change). */
export interface InboxQuery {
	state?: InboxFilter;
	limit?: number;
	/** An opaque cursor from a previous page's `next_cursor`. Hand it back; never construct one. */
	cursor?: string;
}

function inboxSearch(query: InboxQuery): string {
	const params = new URLSearchParams();
	if (query.state !== undefined) params.set('state', query.state);
	if (query.limit !== undefined) params.set('limit', String(query.limit));
	if (query.cursor !== undefined) params.set('cursor', query.cursor);
	const search = params.toString();
	return search === '' ? '' : `?${search}`;
}

/** One page of the caller's own inbox, newest first. */
export async function readInbox(
	req: InboxRequest,
	query: InboxQuery = {},
): Promise<ApiResult<InboxFeed>> {
	const result = await upstreamJSON({
		fetch: req.fetch,
		base: req.base,
		path: `/inbox${inboxSearch(query)}`,
		bearer: req.bearer,
		upstream: UPSTREAM,
	});
	return parsed(result, InboxFeedSchema, UPSTREAM);
}

/**
 * Mark the notifications a panel actually rendered as read — the bell's `onseen` seam.
 *
 * The component hands back its FULL seen set on every close, which is deliberate on both sides: ids
 * the inbox does not hold are simply not there to mark, and re-marking a read row reports zero
 * updates rather than an error. So the caller never has to diff.
 */
export async function markInboxSeen(
	req: InboxRequest,
	notificationIds: string[],
): Promise<ApiResult<MarkResult>> {
	const result = await upstreamJSON({
		fetch: req.fetch,
		base: req.base,
		path: '/inbox/seen',
		init: { method: 'POST', body: JSON.stringify({ notification_ids: notificationIds }) },
		bearer: req.bearer,
		upstream: UPSTREAM,
	});
	return parsed(result, MarkResultSchema, UPSTREAM);
}

/** Dismiss ONE notification — the bell's `ondismiss` seam, keyed by `run_id@STATE` so dismissing a
 *  run's "started" leaves its later "failed" free to arrive. */
export async function dismissNotification(
	req: InboxRequest,
	notificationId: string,
): Promise<ApiResult<DismissResult>> {
	const result = await upstreamJSON({
		fetch: req.fetch,
		base: req.base,
		path: '/inbox/dismiss',
		init: { method: 'POST', body: JSON.stringify({ notification_id: notificationId }) },
		bearer: req.bearer,
		upstream: UPSTREAM,
	});
	return parsed(result, DismissResultSchema, UPSTREAM);
}

/** The two id sets a zone binds into the shared bell. */
export interface InboxReadState {
	seen: string[];
	dismissed: string[];
}

/**
 * Project one page of the inbox onto the bell's read state.
 *
 * IT SPEAKS ONLY FOR THE ROWS THE INBOX HOLDS, which is not the set the bell renders: the panel's
 * rows are `GET /runs`, governed by dataset visibility (every run whose outputs you may read), while
 * the inbox is filled by authorship on terminal states alone. A run you can see but did not author is
 * absent from every page this is ever handed, so it contributes to neither set and its read state
 * stays per tab. That is a property of the two planes rather than of this projection, and it closes
 * when the panel renders inbox rows.
 *
 * `seen` is the whole point and it is exact. `dismissed` comes back EMPTY today and will keep
 * coming back empty until S3, because the service drops dismissed rows from both filters — so a
 * dismissal survives in the inbox (the count is right, and S3's inbox-only badge will be right) but
 * cannot be re-read to re-filter a bell whose rows still come from `GET /runs`. Stated rather than
 * hidden: in S1 a dismissed run reappears in the panel after a reload exactly as it does today, and
 * S3 closes the gap by construction rather than by adding a field — once the panel renders inbox
 * rows, a dismissed row is simply absent and there is no set to reconstruct.
 */
export function inboxReadState(feed: InboxFeed): InboxReadState {
	return {
		seen: feed.notifications.filter((n) => n.seen).map((n) => n.notification_id),
		dismissed: feed.notifications.filter((n) => n.dismissed).map((n) => n.notification_id),
	};
}
