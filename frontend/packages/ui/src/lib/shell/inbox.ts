/** The inbox plane's row shape, structurally — `@rask/ui` must not import `@rask/api`.
 *
 * The same rule `RunStatusLike` follows and for the same reason: the design system takes the shape
 * it renders, never the client that fetched it, so it stays testable with a literal and free of any
 * transport coupling. `@rask/api`'s `InboxNotification` is assignable to this by construction.
 *
 * WHY THIS EXISTS AT ALL — the two planes the bell was conflating (open_notifications.md D8, S3).
 * Until now the panel rendered `GET /runs`, governed by DATASET visibility: every run whose outputs
 * you may read, whoever started it. The badge therefore counted OTHER PEOPLE'S WORK, and the read
 * state the zone persisted spoke for rows the inbox had never heard of — mark one read and it came
 * back unread on the next reload, because there was no pointer to write. Inbox rows are targeted:
 * they exist because something happened that concerns YOU. Rendering them is what makes the badge
 * and the persistence agree, and it closes the gap by construction rather than by adding a field.
 */
export type InboxNotificationLike = {
	/** `run_id@STATE` — the same id the run feed mints (`runNotificationId`), which is what lets the
	 *  two planes share one seen/dismissed vocabulary. */
	notification_id: string;
	/** Why this subject was told. An OPEN string: v2 (project watch) and v3 (governance) add members,
	 *  and a renderer that pinned today's single `author` would break on the day the backend grew one. */
	reason?: string;
	/** The governed object the visibility check ran against. */
	object_id?: string;
	/** The producer's OWN run id — what lineage's detail doors answer to. The graph run id is a
	 *  derived uuid5 and links to nothing. */
	source_run_id?: string | null;
	occurred_at?: string;
	seen?: boolean;
};

/** Newest first, and STABLE: `occurred_at` ties are broken by id so two rows stamped in the same
 *  second cannot swap places between renders and make the list appear to shuffle on its own. */
export function orderedInbox(rows: InboxNotificationLike[]): InboxNotificationLike[] {
	return [...rows].sort((a, b) => {
		const at = Date.parse(a.occurred_at ?? '') || 0;
		const bt = Date.parse(b.occurred_at ?? '') || 0;
		if (at !== bt) return bt - at;
		return a.notification_id.localeCompare(b.notification_id);
	});
}

/** The ids of the rows actually RENDERED, for marking read on close.
 *
 * Sliced to `limit` for the reason the run feed learned the hard way: marking the unsliced list read
 * a FAILED row that was pushed past the cap and never seen — and since this set is what the zone
 * PERSISTS per subject, that failure was gone for good. */
export function inboxSeenOnClose(rows: InboxNotificationLike[], limit: number): string[] {
	return orderedInbox(rows)
		.slice(0, limit)
		.filter((row) => !row.seen)
		.map((row) => row.notification_id);
}

/** The badge, when the server did not hand one over.
 *
 * The SERVER's `unread` is authoritative and must win where present: it counts the whole inbox while
 * these rows are one page, so a reader paging through must not watch the badge shrink. This is the
 * fallback for a page-only caller and for tests. */
export function inboxUnreadCount(
	rows: InboxNotificationLike[],
	locallySeen: string[] = [],
): number {
	const seen = new Set(locallySeen);
	return rows.filter((row) => !row.seen && !seen.has(row.notification_id)).length;
}
