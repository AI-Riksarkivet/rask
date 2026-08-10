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
 * `ondismiss` seam. Server-side only by construction: the inbox is SUBJECT-DERIVED from the
 * verified token's `sub`, and that bearer lives in the sealed httpOnly session cookie the BFF
 * holds, which the browser cannot attach.
 *
 * The full reasoning — why the inbox and the run feed are different sets, and what `null` means —
 * lives once in `microfrontends/home/src/lib/live/inbox.remote.ts`. This file is its sibling.
 */

/**
 * ABSOLUTE, and reading `RASK_GATEWAY_URL` rather than a relative `/api/notifications`. This zone
 * has no `/api` dev proxy at all (it reaches `:8102` through its own BFF), and the zones that do
 * have one mostly point it at `LANCE_BACKEND` (`:8001`, the LINEAGE service) — so a relative call
 * is a 404 waiting to happen. An absolute base also passes the SSR rewrite untouched, since that
 * only rewrites requests to this zone's own origin.
 */
const NOTIFICATIONS_API = `${(env.RASK_GATEWAY_URL ?? 'http://localhost:8888').replace(/\/$/, '')}/api/notifications`;

/** This request's inbox call: the event's fetch and the signed-in bearer, which only exist inside
 *  an app, plus the env-resolved base. */
function inboxRequest() {
	const { fetch, locals } = getRequestEvent();
	return { fetch, base: NOTIFICATIONS_API, bearer: locals.session?.accessToken };
}

/** A notification id as the shared bell mints it (`run_id@STATE`). Parsed rather than trusted:
 *  these arrive from the browser, and an id is a key into the caller's own actor state. */
const NotificationIdSchema = v.pipe(v.string(), v.minLength(1), v.maxLength(512));

/**
 * The read state this subject's inbox already holds, or `null` when it did not answer.
 *
 * `null` is not an error — it is the auth-off dev case and the outage case alike, and both must
 * leave the bell running on the component's own per-tab memory. One page at the service's own
 * ceiling: the bell renders a 20-row window, so a single page covers anything it can display.
 */
export const readInboxState = query(async (): Promise<InboxReadState | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxReadState(result.data) : null;
});

/**
 * The Inbox tab's ROWS — what makes the panel's set and the inbox's set one set.
 *
 * `null` is the un-wired answer (no session, no notifications service), not an error: the bell then
 * renders no tabs and falls back to the run feed, which is what keeps `make dev-zone` working with
 * no cluster behind it.
 */
export const readInboxFeed = query(async (): Promise<InboxPanel | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxPanel(result.data) : null;
});

/**
 * Persist the read set the panel just closed on — the `onseen` seam.
 *
 * The component hands back its FULL seen set every time, and re-sending it is correct rather than
 * wasteful: the service marks only what changed and reports `updated: 0` otherwise, so neither side
 * has to diff and a tab that missed a write repairs itself on the next close.
 */
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
