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
 * `ondismiss` seam. The full reasoning (why the inbox is subject-derived, why none of it is
 * reachable from the browser, why the base is absolute) lives once in
 * `microfrontends/home/src/lib/live/inbox.remote.ts`; this is the same four functions for `explorer`.
 *
 * The base reads `RASK_GATEWAY_URL` and is ABSOLUTE on purpose: this zone runs no `/api` dev proxy
 * (`vite.config.ts`) — `${base}/api/*` is its OWN BFF route tree, which serves the three lance-media
 * doors and nothing else — so a relative `/api/notifications/*` 404s against this app itself.
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
 * `null` is NOT an error — it is the auth-off dev case and the outage case alike, and both must
 * leave the bell rendering on the component's own per-tab memory. One page at the service's own
 * ceiling, which covers everything the 20-row bell can still be displaying.
 */
export const readInboxState = query(async (): Promise<InboxReadState | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxReadState(result.data) : null;
});

/**
 * Persist the read set the panel just closed on — the `onseen` seam.
 *
 * The component hands back its FULL seen set every time, and re-sending it is correct: the service
 * marks only what changed and reports `updated: 0` otherwise, so neither side has to diff and a tab
 * that missed a write repairs itself on the next close.
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

/**
 * The Inbox tab's ROWS — what makes the bell's two read sets one set: the rows here are addressed to
 * this subject, so a mark lands on a pointer that exists and a dismissed row is simply absent.
 *
 * `null` is the un-wired answer, not an error (no session, no notifications service). The bell then
 * renders NO tabs and falls back to the run feed, which is what keeps this zone working with no
 * cluster behind it.
 */
export const readInboxFeed = query(async (): Promise<InboxPanel | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxPanel(result.data) : null;
});
