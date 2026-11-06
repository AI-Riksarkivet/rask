import { command, getRequestEvent, query } from '#lib/reads.svelte.ts';
import { NOTIFICATIONS_API } from '#lib/upstreams.ts';
import {
	dismissNotification,
	INBOX_PAGE_LIMIT_MAX,
	inboxPanel,
	inboxReadState,
	markInboxSeen,
	readInbox,
	readPrefs as readPrefsUpstream,
	writePrefs as writePrefsUpstream,
	type ChannelPreferences,
	type DismissResult,
	type InboxPanel,
	type InboxReadState,
	type MarkResult,
} from '@rask/api/inbox';
import type { ApiResult } from '@rask/api/client';
import * as v from 'valibot';

/**
 * The notifications inbox, the console's one backend: the bell's rows and its `seen`/`dismissed`
 * persistence seam (`@rask/ui`'s `notification-center.svelte`), project watches and channel
 * preferences.
 *
 * The inbox is SUBJECT-DERIVED: `services/notifications` mints the actor id from the verified token's
 * `sub` with no header fallback, so every call below carries the signed-in person's bearer and can
 * only ever address their own inbox. With no session (an auth-off stack) the service serves one
 * anonymous inbox.
 */

/** One inbox call: the browser's fetch, the inbox's base and the signed-in bearer. */
function inboxRequest() {
	const { fetch, locals } = getRequestEvent();
	return { fetch, base: NOTIFICATIONS_API, bearer: locals.session?.accessToken };
}

/** A notification id as the shared bell mints it (`run_id@STATE`). Parsed rather than trusted: an id
 *  is a key into the caller's own actor state. */
const NotificationIdSchema = v.pipe(v.string(), v.minLength(1), v.maxLength(512));

/** The reader of a stack that signs nobody in: the inbox serves it one anonymous inbox. */
export const ANONYMOUS = 'anonymous';

/** A person the bell reads for: the session's `sub`, or the one anonymous inbox of a stack that signs
 *  nobody in. It keys the read's cache, so each person gets an inbox of their own in this tab. */
const BellReaderSchema = v.pipe(v.string(), v.minLength(1), v.maxLength(512));

/** What the bell renders: one page of rows, the badge for the whole inbox, and the read state the
 *  inbox holds for those rows. */
export interface InboxBell {
	panel: InboxPanel;
	held: InboxReadState;
}

/**
 * The bell's one read of `GET /inbox`, or `null` when the inbox did not answer (the outage case and an
 * unrouted stack alike: the bell then renders on its own per-tab memory rather than a blank panel).
 *
 * ONE call answers both halves: the rows and the badge (`inboxPanel`), and the seen/dismissed sets the
 * inbox already holds for them (`inboxReadState`), so a page load sends the request once.
 *
 * Keyed by `reader`, never read from it: the request carries the signed-in person's bearer, and the
 * inbox derives whose inbox it is from that token. The key is what makes a different person a
 * different read, so signing in as someone else never shows the previous person's rows.
 *
 * ONE PAGE, at the service's ceiling (`INBOX_PAGE_LIMIT_MAX`): the bell renders a 20-row window, and
 * an older row outside the page renders unread, the safe direction for a notification surface.
 */
export const readInboxFeed = query(BellReaderSchema, async (_reader): Promise<InboxBell | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? { panel: inboxPanel(result.data), held: inboxReadState(result.data) } : null;
});

/**
 * Persist the read set the panel just closed on — the `onseen` seam.
 *
 * The component hands back its FULL seen set every time, and re-sending it is correct rather than
 * wasteful: the service marks only what changed and reports `updated: 0` otherwise, and ids it does
 * not hold are simply not there to mark. So neither side has to diff, and a tab that missed a write
 * repairs itself on the next close.
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

/** One watched project, as the notifications service records it. */
const WatchListSchema = v.object({
	projects: v.array(v.string()),
	total: v.number(),
});

/**
 * The projects this subject watches — v2 targeting's own surface (S4).
 *
 * `null` is the un-wired answer, not an error: no session (auth-off dev) or no notifications
 * service. The page renders that as "watching is unavailable on this stack" rather than as an empty
 * list, because an empty list is a real and different state — it means you watch nothing.
 */
export const readWatches = query(async (): Promise<string[] | null> => {
	const { fetch, base, bearer } = inboxRequest();
	const result = await requestWatches(fetch, `${base}/watches`, bearer);
	return result;
});

/** Start watching a project. Requires `project#member`, checked by the service against the PROJECT. */
export const watchProject = command(NotificationIdSchema, async (projectId): Promise<boolean> => {
	const { fetch, base, bearer } = inboxRequest();
	const response = await fetch(`${base}/watches/${encodeURIComponent(projectId)}`, {
		method: 'PUT',
		headers: bearer ? { authorization: `Bearer ${bearer}` } : {},
	});
	void readWatches().refresh();
	return response.ok;
});

/** Stop watching. Deliberately NOT membership-gated by the service — someone removed from a project
 *  must still be able to clear a watch they can no longer create. */
export const unwatchProject = command(NotificationIdSchema, async (projectId): Promise<boolean> => {
	const { fetch, base, bearer } = inboxRequest();
	const response = await fetch(`${base}/watches/${encodeURIComponent(projectId)}`, {
		method: 'DELETE',
		headers: bearer ? { authorization: `Bearer ${bearer}` } : {},
	});
	void readWatches().refresh();
	return response.ok;
});

/** The one read, factored out so both the query and its refresh share a single parse. */
async function requestWatches(
	fetch: typeof globalThis.fetch,
	url: string,
	bearer: string | undefined,
): Promise<string[] | null> {
	try {
		const response = await fetch(url, {
			headers: bearer ? { authorization: `Bearer ${bearer}` } : {},
		});
		if (!response.ok) return null;
		return v.parse(WatchListSchema, await response.json()).projects;
	} catch {
		return null;
	}
}

/**
 * This subject's channel preferences — the settings surface's read.
 *
 * `null` is the un-wired answer, not an error, and the page renders it as "channels are unavailable
 * on this stack" rather than as an empty form: an empty form invites you to type into something that
 * cannot save, which is the one outcome the settings index's own comment refuses ("a settings form
 * that silently discards what you type is worse than one that admits it does not exist yet").
 */
export const readPrefs = query(async (): Promise<ChannelPreferences | null> => {
	const result = await readPrefsUpstream(inboxRequest());
	return result.ok ? result.data : null;
});

/** The full preferences document. A PUT replaces it, so the form always sends every field. */
const ChannelPreferencesInput = v.object({
	channels: v.array(v.string()),
	destinations: v.record(v.string(), v.string()),
	digest_seconds: v.nullable(v.number()),
});

export const savePrefs = command(
	ChannelPreferencesInput,
	async (prefs): Promise<ChannelPreferences | null> => {
		const result = await writePrefsUpstream(inboxRequest(), prefs);
		if (!result.ok) return null;
		// Single-flight the read this write invalidates, the estate's mutation shape.
		void readPrefs().refresh();
		return result.data;
	},
);
