import { command, getRequestEvent, query } from '$lib/remote.svelte';
import { NOTIFICATIONS_API } from '$lib/upstreams';
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

/**
 * The read state this subject's inbox already holds, or `null` when it did not answer.
 *
 * NULL IS NOT AN ERROR — it is the auth-off dev case and the outage case, and both must leave the
 * bell rendering on the component's own per-tab memory.
 *
 * ONE PAGE, at the service's own ceiling. The bell renders a 20-row window (`NOTICE_WINDOW`), so
 * the newest 100 delivered notifications cover anything it can still be displaying in every
 * realistic case; the one that escapes is an old FAILED run, which the run feed sorts to the front
 * while the inbox keeps it in date order. That row then renders UNREAD — the safe direction for a
 * notification surface, and the reason this is a bound worth naming rather than a loop worth
 * writing.
 *
 * THE ROWS THIS CAN SPEAK FOR ARE THE ONES YOU AUTHORED, and that is the bound worth reading twice.
 * The panel's rows come from `GET /runs` — governed by DATASET visibility, so every run whose outputs
 * you can `can_get_metadata`, whoever ran it. The inbox is filled by AUTHORSHIP (D4's v1 targeting:
 * `audience_for(notice)` is `(notice.author,)`) and only on COMPLETE/FAIL/ABORT. A stage runner's failed run
 * you can see but did not start therefore has no pointer here, `markSeen` on it stores nothing, and it
 * is unread again after a reload — S1 does not regress that, and it does not fix it either. Closing it
 * is S3 (the panel renders inbox rows) or S4 (project watch widens the audience); it is not a loop to
 * add here, because there is nothing on the wire to read back.
 */
export const readInboxState = query(async (): Promise<InboxReadState | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxReadState(result.data) : null;
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

/**
 * The Inbox tab's ROWS — S3's half of this file, and the one that closes S1's stated gap.
 *
 * `readInboxState` above projects the inbox onto the RUN feed's read state, which could only ever
 * speak for runs you authored: the panel rendered `GET /runs` (dataset-governed, everyone's work)
 * while the inbox holds pointers addressed to you, so marking a row you did not author wrote into a
 * mailbox with no pointer for it and the mark evaporated on reload. Rendering these rows directly is
 * what makes the two sets one set — a dismissed row is simply absent, and there is no read state to
 * reconstruct.
 *
 * `null` is the un-wired answer, not an error: no session (auth-off dev), or no notifications
 * service. The bell then renders NO tabs and falls back to the run feed, which is what keeps a dev
 * server working with no cluster behind it.
 */
export const readInboxFeed = query(async (): Promise<InboxPanel | null> => {
	const result = await readInbox(inboxRequest(), { state: 'all', limit: INBOX_PAGE_LIMIT_MAX });
	return result.ok ? inboxPanel(result.data) : null;
});

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
