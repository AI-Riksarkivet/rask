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
 * `ondismiss` seam, one actor per subject in `services/notifications`.
 *
 * Structurally identical to `home`'s `src/lib/live/inbox.remote.ts`, which carries the full
 * reasoning: why every call is a REMOTE function (the bearer lives in the sealed httpOnly session
 * cookie, so the browser cannot make these calls), why one page at the service's own ceiling is the
 * right bound, and why `null` rather than `[]` is the un-wired answer. Read that module before
 * changing the shape of this one.
 */

/**
 * The gateway's `/api/notifications` row, ABSOLUTE and env-resolved — never a relative `/api/…`.
 *
 * This zone's dev proxy and SSR rewrite are the gateway-facing pair (`VIEWER_BACKEND` /
 * `RASK_GATEWAY_URL`), but `home` and `lakehouse` point the same-looking relative path at
 * `LANCE_GATEWAY_URL` (`:8001`, the lineage service). Spelling the base out keeps this file correct
 * on its own wherever it is copied, and an absolute URL passes the SSR rewrite untouched — it only
 * rewrites requests to this zone's own origin.
 */
const NOTIFICATIONS_API = `${(env.RASK_GATEWAY_URL ?? 'http://localhost:8888').replace(/\/$/, '')}/api/notifications`;

/** This request's inbox call: the event's fetch and the signed-in bearer, which only exist inside an
 *  app, plus the env-resolved base. */
function inboxRequest() {
	const { fetch, locals } = getRequestEvent();
	return { fetch, base: NOTIFICATIONS_API, bearer: locals.session?.accessToken };
}

/** A notification id as the shared bell mints it (`run_id@STATE`). Parsed rather than trusted: these
 *  arrive from the browser, and an id is a key into the caller's own actor state. */
const NotificationIdSchema = v.pipe(v.string(), v.minLength(1), v.maxLength(512));

/** The read state this subject's inbox already holds, or `null` when it did not answer — the
 *  auth-off dev case and the outage case alike, both of which must leave the bell running on the
 *  component's own per-tab memory. */
export const readInboxState = query(async (): Promise<InboxReadState | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxReadState(result.data) : null;
});

/** The Inbox tab's ROWS plus the badge for the whole inbox. `null` = un-wired (no session, no
 *  service), and the bell then renders no tabs and falls back to the run feed. */
export const readInboxFeed = query(async (): Promise<InboxPanel | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxPanel(result.data) : null;
});

/** Persist the read set the panel just closed on — the `onseen` seam. The component hands back its
 *  FULL seen set every time, and re-sending is correct rather than wasteful: the service marks only
 *  what changed and reports `updated: 0` otherwise, so neither side has to diff. */
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
