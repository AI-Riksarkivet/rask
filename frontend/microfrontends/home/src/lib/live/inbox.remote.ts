import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import {
	dismissNotification,
	INBOX_PAGE_LIMIT_MAX,
	inboxReadState,
	markInboxSeen,
	readInbox,
	type DismissResult,
	type InboxReadState,
	type MarkResult,
} from '@rask/api/inbox';
import type { ApiResult } from '@rask/api/client';
import * as v from 'valibot';

/**
 * This zone's door to the notification inbox — the backend half of the shared bell's `onseen` /
 * `ondismiss` seam (`@rask/ui`'s `notification-center.svelte:44-48`, "the persistence seam").
 *
 * WHY HOME, and why exactly one zone (S1; the estate-wide sweep is S3):
 *
 *  - The inbox is SUBJECT-DERIVED. `services/notifications` mints the actor id from the verified
 *    token's `sub` with no header fallback, so a caller with no bearer reads the anonymous inbox.
 *    The bearer lives in the sealed httpOnly session cookie the BFF holds, which is why every call
 *    below is a REMOTE function and none of it is reachable from the browser: the browser cannot
 *    attach the credential the whole surface is keyed on.
 *  - Home OWNS the OIDC BFF — `src/routes/auth` is where the login round-trip runs and the session
 *    cookie is minted — and it is the catch-all zone (base `''`), so it is the one zone a signed-in
 *    user provably passes through. All seven zones hydrate that same cookie (`makeSessionHandle`,
 *    directly or through `makeZoneHooks`), so this is a choice about where the seam is PROVEN
 *    first, not about which zone could hold a bearer.
 *  - It is the only zone with a signed-in Playwright fixture (`e2e/session.ts`), and the acceptance
 *    this slice closes is exactly a browser-level one: seen-state surviving a fresh browser CONTEXT
 *    (`OPEN-WORK.md` B2). `compute` and `studio` ship no `e2e/` at all.
 *  - The watch/prefs settings surface lands here in S4, so the plane has one owner zone from S1
 *    rather than moving between slices.
 *
 * THE PROXY SPLIT, and why nothing here is relative. This zone's `/api` dev proxy targets
 * `LANCE_BACKEND` (`vite.config.ts`) and its SSR rewrite targets `LANCE_GATEWAY_URL`, both
 * defaulting to `http://localhost:8001` — the LINEAGE service, not the gateway
 * (`@rask/api/bff.ts` `DEFAULT_GATEWAY_URL`). So a relative `/api/notifications/*` from this zone
 * 404s against lineage in dev, and survives in prod only because the chart happens to point both
 * names at the same gateway Service (`chart/templates/frontends.yaml`) — a coincidence, not a
 * contract, and a dev/prod split is what it would be the day either name moved. The base is
 * therefore explicit and reads `RASK_GATEWAY_URL`: the variable `compute`, `studio` and the
 * lakehouse's `/api/explorer` door already read, injected into every zone pod by the same chart
 * block. One env, no chart change, and the S1 gateway row is the whole address. The absolute URL
 * also passes the SSR rewrite untouched — it only rewrites requests to this zone's OWN origin
 * (`@rask/api/gateway.ts`), which is what keeps `LANCE_GATEWAY_URL` out of this path entirely.
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
 * NULL IS NOT AN ERROR — it is the auth-off dev case and the outage case, and both must leave the
 * bell rendering on the component's own per-tab memory. A zone running with no session and no
 * notifications service behaves exactly as it did before this file existed.
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
 * `audience_for(notice)` is `(notice.author,)`) and only on COMPLETE/FAIL/ABORT. A mover's failed run
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
