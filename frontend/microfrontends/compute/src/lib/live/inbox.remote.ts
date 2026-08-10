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
 * `ondismiss` seam. Server-side only by necessity: `services/notifications` derives the actor from
 * the verified token's `sub`, and the bearer lives in the sealed httpOnly session cookie this zone's
 * `makeSessionHandle` hydrates, which the browser cannot attach.
 *
 * The full reasoning — why the inbox is subject-derived, why the base is absolute, and what the
 * authorship bound costs — lives once in `microfrontends/home/src/lib/live/inbox.remote.ts`.
 */

/** ABSOLUTE, and reading `RASK_GATEWAY_URL` — the same variable this zone's `hooks.server.ts` uses
 *  for the Ray plane. A relative `/api/notifications` is not safe estate-wide: several zones proxy
 *  `/api` at `LANCE_GATEWAY_URL` (`:8001`, the lineage service), where it 404s. An absolute URL also
 *  passes the SSR gateway rewrite untouched, since that only rewrites this zone's own origin. */
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

/** The read state this subject's inbox already holds, or `null` when it did not answer. `null` is
 *  not an error — it is the auth-off dev case and the outage case alike, and both must leave the
 *  bell on its own per-tab memory. One page at the service's ceiling; the bell renders a 20-row
 *  window, so a further page could not change what it shows. */
export const readInboxState = query(async (): Promise<InboxReadState | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxReadState(result.data) : null;
});

/** The Inbox tab's ROWS — rows addressed to this subject, as opposed to the Activity tab's
 *  dataset-governed run feed. `null` is the un-wired answer (no session, no service): the bell then
 *  renders no tabs and falls back to the run feed, which is what keeps `make dev-zone ZONE=compute`
 *  working with no cluster behind it. */
export const readInboxFeed = query(async (): Promise<InboxPanel | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxPanel(result.data) : null;
});

/** Persist the read set the panel just closed on — the `onseen` seam. The component hands back its
 *  FULL seen set every time, and re-sending it is correct rather than wasteful: the service marks
 *  only what changed and reports `updated: 0` otherwise, so neither side has to diff and a tab that
 *  missed a write repairs itself on the next close. */
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
