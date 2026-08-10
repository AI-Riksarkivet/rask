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
 * `ondismiss` seam. Server-side only: the bearer lives in the sealed httpOnly session cookie, and
 * the service derives the actor from the token's `sub` with no header fallback.
 *
 * The base is ABSOLUTE and reads `RASK_GATEWAY_URL` — never a relative `/api/*`. See
 * `microfrontends/home/src/lib/live/inbox.remote.ts` for the full reasoning (the proxy split: some
 * zones' `/api` targets `LANCE_GATEWAY_URL` on :8001, the lineage service, so a relative call 404s).
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
 * `null` is NOT an error: it is the auth-off dev case and the outage case alike, and both must
 * leave the bell on its own per-tab memory. One page at the service's own ceiling — the bell
 * renders a 20-row window, so a loop would buy nothing the panel can display.
 */
export const readInboxState = query(async (): Promise<InboxReadState | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxReadState(result.data) : null;
});

/**
 * The Inbox tab's ROWS — what the bell renders instead of the run feed once this zone is wired.
 *
 * `null` is the un-wired answer (no session, no notifications service): the bell then renders NO
 * tabs and falls back to the Activity feed, which is what keeps `make dev-zone ZONE=models` working
 * with no cluster behind it.
 */
export const readInboxFeed = query(async (): Promise<InboxPanel | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxPanel(result.data) : null;
});

/** Persist the read set the panel just closed on — the `onseen` seam. The component hands back its
 *  FULL seen set every time; the service marks only what changed, so neither side has to diff. */
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
