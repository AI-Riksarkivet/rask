import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import {
	dismissNotification,
	INBOX_PAGE_LIMIT_MAX,
	inboxPanel,
	inboxReadState,
	markInboxSeen,
	readInbox,
	type DismissResult,
	type InboxPanel,
	type InboxReadState,
	type MarkResult,
} from '@rask/api/inbox';
import type { ApiResult } from '@rask/api/client';
import * as v from 'valibot';

/**
 * This zone's door to the notification inbox — the backend half of the shared bell's `onseen` /
 * `ondismiss` seam, bound to `services/notifications` (one inbox actor per subject).
 *
 * Server-side only, because the surface is SUBJECT-DERIVED: the service mints the actor id from the
 * verified token's `sub`, and that bearer lives in the sealed httpOnly session cookie only a remote
 * function can reach. `home`'s `$lib/live/inbox.remote.ts` carries the full rationale (why every read
 * returns `null` rather than throwing, and what the inbox can and cannot speak for) — this is the
 * same module, bound to this zone's request event.
 */

/**
 * ABSOLUTE, and read off `RASK_GATEWAY_URL` rather than written relative. This zone's `/api` dev
 * proxy and its SSR rewrite both target `LANCE_GATEWAY_URL` (`:8001`, the LINEAGE service), so a
 * relative `/api/notifications/*` from here 404s against lineage in dev. An absolute gateway URL also
 * passes the SSR rewrite untouched — it only rewrites calls to this zone's own origin.
 */
const NOTIFICATIONS_API = `${(env.RASK_GATEWAY_URL ?? 'http://localhost:8888').replace(/\/$/, '')}/api/notifications`;

/** This request's inbox call: the event's fetch and the signed-in bearer, plus the env-resolved base. */
function inboxRequest() {
	const { fetch, locals } = getRequestEvent();
	return { fetch, base: NOTIFICATIONS_API, bearer: locals.session?.accessToken };
}

/** A notification id as the shared bell mints it (`run_id@STATE`). Parsed rather than trusted: these
 *  arrive from the browser, and an id is a key into the caller's own actor state. */
const NotificationIdSchema = v.pipe(v.string(), v.minLength(1), v.maxLength(512));

/**
 * The read state this subject's inbox already holds, or `null` when it did not answer.
 *
 * `null` IS NOT AN ERROR — it is the auth-off dev case and the outage case alike, and both must leave
 * the bell running on the component's own per-tab memory. One page at the service's own ceiling; the
 * bell renders a 20-row window, so the newest 100 delivered rows cover anything it can display.
 */
export const readInboxState = query(async (): Promise<InboxReadState | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxReadState(result.data) : null;
});

/**
 * The Inbox tab's ROWS — what the bell renders instead of projecting the run feed onto a read state.
 *
 * `null` is the un-wired answer, not an error: no session (auth-off dev) or no notifications service,
 * and the bell renders NO tabs and falls back to the run feed — which is what keeps
 * `make dev-zone ZONE=lakehouse` working with no cluster behind it.
 */
export const readInboxFeed = query(async (): Promise<InboxPanel | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxPanel(result.data) : null;
});

/** Persist the read set the panel just closed on — the `onseen` seam. The component hands back its
 *  FULL seen set every time, and re-sending it is correct rather than wasteful: the service marks
 *  only what changed and reports `updated: 0` otherwise, so neither side has to diff. */
export const markSeen = command(
	v.array(NotificationIdSchema),
	(notificationIds): Promise<ApiResult<MarkResult>> =>
		markInboxSeen(inboxRequest(), notificationIds),
);

/** Persist one dismissal — the `ondismiss` seam. Keyed by `run_id@STATE`, so dismissing a run's
 *  "started" leaves its later "failed" free to arrive. */
export const dismiss = command(
	NotificationIdSchema,
	(notificationId): Promise<ApiResult<DismissResult>> =>
		dismissNotification(inboxRequest(), notificationId),
);
